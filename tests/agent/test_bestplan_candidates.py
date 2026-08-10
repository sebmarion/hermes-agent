from __future__ import annotations

import hashlib
import inspect
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "candidate@example.test")
    _git(path, "config", "user.name", "Candidate Test")
    (path / ".gitignore").write_text("ignored.env\n", encoding="utf-8")
    (path / "allowed").mkdir()
    (path / "allowed" / "base.txt").write_text("base\n", encoding="utf-8")
    (path / "outside.txt").write_text("outside\n", encoding="utf-8")
    _git(path, "add", ".gitignore", "allowed/base.txt", "outside.txt")
    _git(path, "commit", "-qm", "base")
    (path / "ignored.env").write_text("ambient-only\n", encoding="utf-8")
    return path


def _snapshot(repo: Path):
    from agent import bestplan_source as source

    identity = source.resolve_repo_identity(str(repo))
    return source.capture_source_snapshot(
        identity,
        time.monotonic() + source.DEFAULT_SOURCE_OPERATION_SECONDS,
    )


def _candidates():
    from agent import bestplan_candidates

    return bestplan_candidates


def _fake_controller_runtime(tmp_path: Path, name: str = "runtime"):
    root = tmp_path / name
    interpreter = root / "bin" / "python3.11"
    stdlib = root / "lib" / "python3.11"
    dynload = stdlib / "lib-dynload"
    site = stdlib / "site-packages"
    for path in (interpreter.parent, dynload, site):
        path.mkdir(parents=True, exist_ok=True)
    interpreter.write_bytes(b"pinned interpreter\n")
    interpreter.chmod(0o755)
    (stdlib / "stdlib.py").write_bytes(b"stdlib\n")
    (dynload / "extension.so").write_bytes(b"extension\n")
    (site / "dependency.py").write_bytes(b"dependency\n")
    native = root / "lib" / "libpython3.11.dylib"
    native.write_bytes(b"native runtime\n")
    return interpreter, (stdlib, dynload, site, native)


def _candidate_environment(
    workspace: Path,
    runtime: Path,
    scratch: Path,
    broker_fd: int,
) -> dict[str, str]:
    return {
        "HOME": str(runtime.resolve()),
        "HERMES_BESTPLAN_BROKER_FD": str(broker_fd),
        "HERMES_BESTPLAN_CHILD": "1",
        "HERMES_HOME": str(runtime.resolve()),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "TERMINAL_CWD": str(workspace.resolve()),
        "TMPDIR": str(scratch.resolve()),
    }


def _controller_identity(controller: Path, *, repository_id: str = "repo-test"):
    from agent.bestplan_contract import ControllerIdentity
    from agent.bestplan_sandbox import candidate_controller_artifact_sha256

    return ControllerIdentity(
        repository_id=repository_id,
        controller_id="controller-n-1",
        release_oid="d" * 40,
        artifact_sha256=candidate_controller_artifact_sha256(controller),
    )


def _spec(candidates, **overrides):
    values = {
        "plan_id": "bp-plan-1",
        "candidate_id": "candidate-code-1",
        "slice_id": "code",
        "goal": "Create the declared artifact",
        "allowed_paths": ("allowed/",),
        "read_only": False,
        "expected_artifacts": ("allowed/result.txt",),
        "model": "test/model",
        "request_budget": 3,
        "token_budget": 4096,
        "expires_at": 2_000_000_000,
        "max_iterations": 7,
        "max_output_tokens": 512,
        "toolsets": ("file",),
    }
    values.update(overrides)
    return candidates.CandidateSpec(**values)


def _freeze(candidates, snapshot, attempt, spec, *, raw_receipt):
    sealed = candidates.seal_candidate_attempt(attempt)
    return candidates._freeze_sealed_candidate_for_test(
        snapshot, sealed, spec, raw_receipt=raw_receipt,
    )


def test_attempt_exports_captured_base_after_head_moves_and_never_imports_git_or_ignored(
    tmp_path,
):
    candidates = _candidates()
    repo = _repo(tmp_path / "repo")
    snapshot = _snapshot(repo)
    (repo / "allowed" / "base.txt").write_text("later head\n", encoding="utf-8")
    _git(repo, "add", "allowed/base.txt")
    _git(repo, "commit", "-qm", "advance head")

    first = candidates.create_candidate_attempt(
        snapshot,
        plan_id="bp-plan-1",
        slice_id="code",
        attempts_root=tmp_path / "attempts",
        attempt_id="attempt-one",
    )
    second = candidates.create_candidate_attempt(
        snapshot,
        plan_id="bp-plan-1",
        slice_id="code",
        attempts_root=tmp_path / "attempts",
        attempt_id="attempt-two",
    )

    assert first.root != second.root
    assert first.source_dir.is_dir() and not first.source_dir.is_symlink()
    assert (first.source_dir / "allowed" / "base.txt").read_text() == "base\n"
    assert not (first.source_dir / "ignored.env").exists()
    assert not any(path.name == ".git" for path in first.source_dir.rglob("*"))
    assert first.runtime_dir.parent == first.root
    assert first.scratch_dir.parent == first.root
    assert first.control_dir.parent == first.root
    assert first.control_dir != first.runtime_dir
    assert first.ref_name.startswith("refs/hermes-bestplan/")
    assert first.base_ref_name.startswith("refs/hermes-bestplan-bases/")
    assert _git(repo, "rev-parse", first.base_ref_name) == snapshot.head_oid


def test_attempt_ids_and_layout_are_unique_and_non_reusable(tmp_path):
    candidates = _candidates()
    snapshot = _snapshot(_repo(tmp_path / "repo"))
    first = candidates.create_candidate_attempt(
        snapshot,
        plan_id="bp-plan-1",
        slice_id="code",
        attempts_root=tmp_path / "attempts",
    )
    second = candidates.create_candidate_attempt(
        snapshot,
        plan_id="bp-plan-1",
        slice_id="code",
        attempts_root=tmp_path / "attempts",
    )
    assert first.attempt_id != second.attempt_id
    assert first.root != second.root
    with pytest.raises(candidates.CandidateValidationError, match="already exists"):
        candidates.create_candidate_attempt(
            snapshot,
            plan_id="bp-plan-1",
            slice_id="code",
            attempts_root=tmp_path / "attempts",
            attempt_id=first.attempt_id,
        )


def test_attempt_and_ref_identifiers_reject_ambiguous_or_control_text(tmp_path):
    candidates = _candidates()
    snapshot = _snapshot(_repo(tmp_path / "repo"))
    for invalid in ("", ".", "..", "slash/value", "back\\slash", "nul\x00value", "@{"):
        with pytest.raises(candidates.CandidateValidationError, match="identifier"):
            candidates.create_candidate_attempt(
                snapshot,
                plan_id="bp-plan-1",
                slice_id="code",
                attempts_root=tmp_path / "attempts",
                attempt_id=invalid,
            )


def test_raw_candidate_paths_reject_case_and_unicode_aliases():
    candidates = _candidates()
    with pytest.raises(candidates.CandidateValidationError, match="alias"):
        candidates.validate_raw_candidate_paths((b"allowed/Result.txt", b"allowed/result.txt"))
    with pytest.raises(candidates.CandidateValidationError, match="alias"):
        candidates.validate_raw_candidate_paths((
            "allowed/Caf\N{LATIN SMALL LETTER E WITH ACUTE}.txt".encode(),
            "allowed/Cafe\N{COMBINING ACUTE ACCENT}.txt".encode(),
        ))


def test_worker_environment_is_allowlisted_and_has_no_provider_or_deploy_credentials(
    tmp_path, monkeypatch,
):
    candidates = _candidates()
    attempt = candidates.create_candidate_attempt(
        _snapshot(_repo(tmp_path / "repo")),
        plan_id="bp-plan-1",
        slice_id="code",
        attempts_root=tmp_path / "attempts",
        attempt_id="environment",
    )
    controller = tmp_path / "controller-n-1"
    controller.mkdir()
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "SSH_AUTH_SOCK",
        "AWS_SECRET_ACCESS_KEY",
        "CLOUDFLARE_API_TOKEN",
        "VERCEL_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "PYTHONPATH",
        "GIT_CONFIG_PARAMETERS",
    ):
        monkeypatch.setenv(key, f"sentinel-{key}")

    environment = candidates.build_candidate_environment(
        attempt,
        controller_source=controller,
        broker_fd=17,
    )

    assert set(environment) == set(candidates.CANDIDATE_ENVIRONMENT_KEYS)
    assert environment["HERMES_HOME"] == str(attempt.runtime_dir)
    assert environment["TERMINAL_CWD"] == str(attempt.source_dir)
    assert environment["HERMES_BESTPLAN_BROKER_FD"] == "17"
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["PYTHONSAFEPATH"] == "1"
    assert "PYTHONPATH" not in environment
    assert "sentinel" not in json.dumps(environment)
    assert str(attempt.source_dir) not in environment.get("PATH", "")


def test_candidate_spec_rejects_process_capable_or_mixed_toolsets():
    candidates = _candidates()
    for toolsets in (("terminal",), ("terminal", "file"), ("file", "web")):
        with pytest.raises(candidates.CandidateValidationError, match="toolset"):
            _spec(candidates, toolsets=toolsets)
    with pytest.raises(candidates.CandidateValidationError, match="read-only"):
        _spec(candidates, read_only=True, toolsets=("file",))
    assert _spec(
        candidates,
        read_only=True,
        allowed_paths=(),
        toolsets=("read_only_files",),
    ).toolsets == ("read_only_files",)


def test_candidate_spec_freezes_model_expiry_artifact_and_lease_invariants():
    candidates = _candidates()
    now = int(time.time())
    for overrides, message in (
        ({"model": ""}, "model"),
        ({"model": "m" * 1025}, "model"),
        ({"expires_at": now - 1}, "expiry"),
        ({"allowed_paths": ()}, "write lease"),
        ({"expected_artifacts": ()}, "artifact"),
        ({
            "read_only": True,
            "toolsets": ["read_only_files"],
            "allowed_paths": ["allowed/"],
        }, "read-only"),
    ):
        with pytest.raises(candidates.CandidateValidationError, match=message):
            _spec(candidates, **overrides)

    mutable_toolsets = ["file"]
    spec = _spec(candidates, toolsets=mutable_toolsets)
    mutable_toolsets.append("read_only_files")
    assert spec.toolsets == ("file",)


def test_root_write_lease_covers_every_validated_candidate_path(tmp_path):
    candidates = _candidates()
    snapshot = _snapshot(_repo(tmp_path / "repo"))
    spec = _spec(
        candidates,
        allowed_paths=(".",),
        expected_artifacts=("result.txt",),
    )
    attempt = candidates.create_candidate_attempt(
        snapshot,
        plan_id=spec.plan_id,
        slice_id=spec.slice_id,
        attempts_root=tmp_path / "attempts",
        attempt_id="root-lease",
    )
    (attempt.source_dir / "result.txt").write_text("ok\n")

    frozen = _freeze(
        candidates, snapshot, attempt, spec, raw_receipt={"status": "ok"},
    )

    assert frozen.changed_paths == (b"result.txt",)


def test_attempt_root_rejects_primary_repository_or_controller_overlap_before_mutation(
    tmp_path,
):
    candidates = _candidates()
    repo = _repo(tmp_path / "repo")
    snapshot = _snapshot(repo)
    controller = tmp_path / "controller"
    (controller / "agent").mkdir(parents=True)
    (controller / "agent" / "bestplan_worker.py").write_text("# worker\n")

    for attempts_root in (repo / "attempts", repo / ".git" / "attempts", controller / "attempts"):
        with pytest.raises(candidates.CandidateValidationError, match="attempt root overlap"):
            candidates.run_and_freeze_candidate(
                snapshot=snapshot,
                spec=_spec(candidates),
                attempts_root=attempts_root,
                controller_source=controller,
                controller_python=Path(sys.executable),
                runtime_read_paths=(),
                expected_controller=_controller_identity(
                    controller, repository_id=snapshot.repo.repository_id,
                ),
                authority_client=object(),
                timeout_seconds=2,
                attempt_id="overlap-attempt",
            )
        assert not attempts_root.exists()


@pytest.mark.parametrize(
    "timeout_seconds",
    (True, float("nan"), float("inf"), -float("inf"), 0, 86_401),
)
def test_public_runner_rejects_invalid_timeout_before_attempt_or_ref_creation(
    tmp_path, timeout_seconds,
):
    candidates = _candidates()
    repo = _repo(tmp_path / "repo")
    snapshot = _snapshot(repo)
    controller = tmp_path / "controller"
    (controller / "agent").mkdir(parents=True)
    (controller / "agent" / "bestplan_worker.py").write_text("# worker\n")
    attempts_root = tmp_path / "attempts"

    with pytest.raises(candidates.CandidateValidationError, match="timeout"):
        candidates.run_and_freeze_candidate(
            snapshot=snapshot,
            spec=_spec(candidates),
            attempts_root=attempts_root,
            controller_source=controller,
            controller_python=Path(sys.executable),
            runtime_read_paths=(),
            expected_controller=_controller_identity(
                controller, repository_id=snapshot.repo.repository_id,
            ),
            authority_client=object(),
            timeout_seconds=timeout_seconds,
            attempt_id="invalid-timeout",
        )

    assert not attempts_root.exists()
    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", "refs/hermes-bestplan"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert refs.stdout == ""


def test_public_runner_rechecks_spec_expiry_before_attempt_or_ref_creation(
    tmp_path, monkeypatch,
):
    candidates = _candidates()
    from agent import bestplan_sandbox as sandbox

    repo = _repo(tmp_path / "repo")
    snapshot = _snapshot(repo)
    controller = tmp_path / "controller"
    (controller / "agent").mkdir(parents=True)
    (controller / "agent" / "bestplan_worker.py").write_text("# worker\n")
    expires_at = int(time.time()) + 10
    spec = _spec(candidates, expires_at=expires_at)
    attempts_root = tmp_path / "attempts"
    monkeypatch.setattr(candidates.time, "time", lambda: expires_at + 1.0)
    monkeypatch.setattr(
        sandbox,
        "create_bestplan_candidate_sandbox_launch",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("expired candidate reached sandbox creation")
        ),
    )

    with pytest.raises(candidates.CandidateValidationError, match="expired"):
        candidates.run_and_freeze_candidate(
            snapshot=snapshot,
            spec=spec,
            attempts_root=attempts_root,
            controller_source=controller,
            controller_python=Path(sys.executable),
            runtime_read_paths=(),
            expected_controller=_controller_identity(
                controller, repository_id=snapshot.repo.repository_id,
            ),
            authority_client=object(),
            timeout_seconds=2,
            attempt_id="expired-before-attempt",
        )

    assert not attempts_root.exists()
    assert not _git(
        repo, "for-each-ref", "--format=%(refname)", "refs/hermes-bestplan",
    )


def test_broker_channel_allocation_failure_precedes_attempt_and_base_ref_creation(
    tmp_path, monkeypatch,
):
    candidates = _candidates()
    repo = _repo(tmp_path / "repo")
    snapshot = _snapshot(repo)
    controller = tmp_path / "controller"
    (controller / "agent").mkdir(parents=True)
    (controller / "agent" / "bestplan_worker.py").write_text("# worker\n")
    attempts_root = tmp_path / "attempts"
    monkeypatch.setattr(
        candidates.socket,
        "socketpair",
        lambda: (_ for _ in ()).throw(OSError("injected channel allocation failure")),
    )

    with pytest.raises(candidates.CandidateExecutionError, match="channel"):
        candidates.run_and_freeze_candidate(
            snapshot=snapshot,
            spec=_spec(candidates),
            attempts_root=attempts_root,
            controller_source=controller,
            controller_python=Path(sys.executable),
            runtime_read_paths=(),
            expected_controller=_controller_identity(
                controller, repository_id=snapshot.repo.repository_id,
            ),
            authority_client=object(),
            timeout_seconds=2,
            attempt_id="socketpair-failure",
        )

    assert not attempts_root.exists()
    assert not _git(
        repo, "for-each-ref", "--format=%(refname)", "refs/hermes-bestplan",
    )


def test_candidate_sandbox_policy_is_default_deny_and_binds_only_explicit_roots(
    tmp_path, monkeypatch,
):
    from agent import bestplan_sandbox as sandbox

    source_dir = tmp_path / "attempt" / "source"
    runtime_dir = tmp_path / "attempt" / "runtime"
    scratch_dir = tmp_path / "attempt" / "scratch"
    controller = tmp_path / "controller-n-1"
    controller_python, runtime_paths = _fake_controller_runtime(tmp_path)
    primary = tmp_path / "primary-checkout"
    sibling = tmp_path / "sibling"
    for path in (
        source_dir / "allowed",
        runtime_dir,
        scratch_dir,
        controller,
        primary,
        sibling,
    ):
        path.mkdir(parents=True, exist_ok=True)
    (controller / "agent").mkdir()
    (controller / "agent" / "bestplan_worker.py").write_text("# pinned worker\n")
    monkeypatch.setattr(sandbox, "_macos_sandbox_available", lambda: True)

    parent_channel, child_channel = socket.socketpair()
    launch = sandbox.create_bestplan_candidate_sandbox_launch(
        workspace=source_dir,
        allowed_paths=("allowed/",),
        read_only=False,
        runtime_dir=runtime_dir,
        scratch_dir=scratch_dir,
        control_dir=tmp_path / "attempt" / "control",
        controller_source=controller,
        controller_python=controller_python,
        runtime_read_paths=runtime_paths,
        enabled_toolsets=("file",),
        expected_controller=_controller_identity(controller),
        worker_environment=_candidate_environment(
            source_dir, runtime_dir, scratch_dir, child_channel.fileno(),
        ),
        broker_fd=child_channel.fileno(),
    )
    try:
        policy = launch.profile_path.read_text(encoding="utf-8")
        assert "(deny default)" in policy
        assert "(deny network*)" in policy
        assert "(deny mach-lookup)" in policy
        assert "(deny signal)" in policy
        assert "(deny process-info*)" in policy
        assert "(deny process-fork)" in policy
        assert f"(subpath {json.dumps(str(source_dir.resolve()))})" in policy
        assert f"(subpath {json.dumps(str(controller.resolve()))})" in policy
        for dependency in runtime_paths:
            operation = "literal" if dependency.is_file() else "subpath"
            assert (
                f"({operation} {json.dumps(str(dependency.resolve()))})" in policy
            )
        assert f"(subpath {json.dumps(str((source_dir / 'allowed').resolve()))})" in policy
        assert str(primary.resolve()) not in policy
        assert str(sibling.resolve()) not in policy
    finally:
        launch.close()
        parent_channel.close()
        child_channel.close()


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file(),
    reason="requires the real macOS sandbox-exec backend",
)
def test_real_candidate_profile_boots_controller_and_enforces_exact_boundaries(tmp_path):
    import ctypes
    import errno
    import shutil
    import tempfile

    from agent import bestplan_sandbox as sandbox

    attempt = tmp_path / "attempt"
    source = attempt / "source"
    runtime = attempt / "runtime"
    scratch = attempt / "scratch"
    control = attempt / "control"
    lease = source / "allowed"
    unrelated = tmp_path / "unrelated"
    for path in (lease, runtime, scratch, control, unrelated):
        path.mkdir(parents=True)
    (source / "not-leased.txt").write_text("source but not leased\n")
    exact_file = source / "exact.txt"
    exact_file.write_text("before\n")
    private_file = unrelated / "private.txt"
    private_file.write_text("must remain unreadable\n")

    controller = Path(__file__).resolve().parents[2]
    launcher = Path(sys.executable).absolute()
    interpreter = launcher.resolve(strict=True)
    dependencies = sandbox.pinned_candidate_runtime_paths(launcher)
    resolved = {
        "root": source.resolve(),
        "runtime": runtime.resolve(),
        "scratch": scratch.resolve(),
        "control": control.resolve(),
        "controller": controller.resolve(),
        "interpreter_launcher": launcher,
        "interpreter": interpreter,
        "dependencies": dependencies,
        "leases": (lease.resolve(), exact_file.resolve()),
        "toolsets": ("file",),
        "bootstrap_sha256": hashlib.sha256(
            sandbox.CANDIDATE_BOOTSTRAP.encode("utf-8"),
        ).hexdigest(),
        "worker_command": (),
        "artifact_identity": {},
    }
    profile = sandbox._candidate_profile_text(resolved)
    bound, bound_profile, policy_digest = sandbox._bind_profile_identity({}, resolved)
    assert bound_profile == profile
    assert bound["profile_sha256"] == hashlib.sha256(profile.encode()).hexdigest()
    assert len(policy_digest) == 64
    profile_path = control / "candidate.sb"
    profile_path.write_text(profile, encoding="utf-8")

    libc = ctypes.CDLL(None)
    bootstrap_look_up = libc.bootstrap_look_up
    bootstrap_look_up.argtypes = [
        ctypes.c_uint,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_uint),
    ]
    bootstrap_look_up.restype = ctypes.c_int
    bootstrap_port = ctypes.c_uint.in_dll(libc, "bootstrap_port").value
    mach_service = None
    for candidate in ("com.apple.cfprefsd.agent", "com.apple.system.logger"):
        service_port = ctypes.c_uint(0)
        if bootstrap_look_up(
            bootstrap_port, candidate.encode(), ctypes.byref(service_port),
        ) == 0 and service_port.value:
            mach_service = candidate
            break
    if mach_service is None:
        pytest.skip("no parent-visible Mach service is available for the denial probe")

    inherited_parent, inherited_child = socket.socketpair()
    unrelated_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    socket_root = Path(tempfile.mkdtemp(prefix="bp-sock-", dir="/private/tmp"))
    unrelated_socket_path = socket_root / "service.sock"
    unrelated_socket.bind(str(unrelated_socket_path))
    unrelated_socket.listen(1)
    tcp_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp_listener.bind(("127.0.0.1", 0))
    tcp_listener.listen(1)

    config = {
        "broker_fd": inherited_child.fileno(),
        "controller": str(controller),
        "dependencies": [str(path) for path in dependencies],
        "interpreter": str(interpreter),
        "lease_file": str(lease / "created.txt"),
        "exact_file": str(exact_file),
        "mach_service": mach_service,
        "not_leased_file": str(source / "not-leased-created.txt"),
        "parent_pid": os.getpid(),
        "private_file": str(private_file),
        "runtime_file": str(runtime / "created.txt"),
        "scratch_file": str(scratch / "created.txt"),
        "tcp_port": tcp_listener.getsockname()[1],
        "unix_socket": str(unrelated_socket_path),
        "unrelated_write": str(unrelated / "created.txt"),
    }
    probe = r'''
import ctypes, errno, json, os, socket, subprocess, sys

config = json.loads(sys.argv[1])
sys.path[:] = [config["controller"], *config["dependencies"]]
from agent import bestplan_worker as worker
worker._install_bestplan_import_guard()
worker._brokered_agent_class(object())
file_tools = worker._CandidateFileTools(
    workspace=os.getcwd(), allowed_paths=["exact.txt"], read_only=False,
)
file_tools.write({"path": "exact.txt", "content": "written\n"})
file_tools.patch({
    "path": "exact.txt",
    "old_string": "written",
    "new_string": "patched",
})

results = {}
def denied(name, operation):
    try:
        value = operation()
    except OSError as exc:
        results[name] = {"denied": True, "errno": exc.errno}
    else:
        results[name] = {"denied": False, "value": value}

channel = socket.socket(fileno=config["broker_fd"])
channel.sendall(b"candidate-broker-ok")
for key in ("lease_file", "runtime_file", "scratch_file"):
    with open(config[key], "w", encoding="utf-8") as handle:
        handle.write(key + "\n")

denied("private_read", lambda: open(config["private_file"], "rb").read())
denied("system_read", lambda: open("/etc/hosts", "rb").read())
denied(
    "source_write",
    lambda: open(config["not_leased_file"], "w", encoding="utf-8").write("no"),
)
denied(
    "unrelated_write",
    lambda: open(config["unrelated_write"], "w", encoding="utf-8").write("no"),
)

def connect_unix():
    candidate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        candidate.connect(config["unix_socket"])
        return "connected"
    finally:
        candidate.close()
denied("unix_connect", connect_unix)

def connect_tcp():
    candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        candidate.connect(("127.0.0.1", config["tcp_port"]))
        return "connected"
    finally:
        candidate.close()
denied("tcp_connect", connect_tcp)

def fork_child():
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
    return pid
denied("fork", fork_child)

def spawn_child():
    pid = os.posix_spawn(
        config["interpreter"],
        [config["interpreter"], "-I", "-S", "-B", "-c", "pass"],
        os.environ,
    )
    os.waitpid(pid, 0)
    return pid
denied("posix_spawn", spawn_child)
denied(
    "subprocess",
    lambda: subprocess.run(
        [config["interpreter"], "-I", "-S", "-B", "-c", "pass"],
        check=False,
    ).returncode,
)
denied("parent_signal", lambda: os.kill(config["parent_pid"], 0))

libc = ctypes.CDLL(None)
lookup = libc.bootstrap_look_up
lookup.argtypes = [ctypes.c_uint, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint)]
lookup.restype = ctypes.c_int
bootstrap_port = ctypes.c_uint.in_dll(libc, "bootstrap_port").value
service_port = ctypes.c_uint(0)
mach_rc = lookup(
    bootstrap_port, config["mach_service"].encode(), ctypes.byref(service_port),
)
results["mach_lookup"] = {"rc": mach_rc, "port": service_port.value}
print(json.dumps(results, sort_keys=True, separators=(",", ":")))
'''
    environment = _candidate_environment(
        source, runtime, scratch, inherited_child.fileno(),
    )
    try:
        result = subprocess.run(
            [
                "/usr/bin/sandbox-exec", "-f", str(profile_path),
                str(interpreter), "-I", "-S", "-B", "-c", probe,
                json.dumps(config, sort_keys=True, separators=(",", ":")),
            ],
            cwd=source,
            env=environment,
            pass_fds=(inherited_child.fileno(),),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        inherited_child.close()
        inherited_parent.settimeout(5)
        assert inherited_parent.recv(64) == b"candidate-broker-ok", (
            result.returncode, result.stdout, result.stderr,
        )
    finally:
        inherited_parent.close()
        try:
            inherited_child.close()
        except OSError:
            pass
        unrelated_socket.close()
        tcp_listener.close()
        shutil.rmtree(socket_root)

    assert result.returncode == 0, result.stderr
    assert len(result.stderr.encode("utf-8")) <= 1024
    assert str(private_file) not in result.stderr
    assert "must remain unreadable" not in result.stderr
    observed = json.loads(result.stdout)
    for name in (
        "private_read",
        "system_read",
        "source_write",
        "unrelated_write",
        "unix_connect",
        "tcp_connect",
        "fork",
        "posix_spawn",
        "subprocess",
        "parent_signal",
    ):
        assert observed[name]["denied"] is True, (name, observed[name])
        assert observed[name]["errno"] in (errno.EACCES, errno.EPERM)
    assert observed["mach_lookup"]["rc"] != 0
    assert observed["mach_lookup"]["port"] == 0
    assert (lease / "created.txt").read_text() == "lease_file\n"
    assert exact_file.read_text() == "patched\n"
    assert (runtime / "created.txt").read_text() == "runtime_file\n"
    assert (scratch / "created.txt").read_text() == "scratch_file\n"


def test_candidate_sandbox_policy_digest_changes_with_controller_or_runtime_root(
    tmp_path, monkeypatch,
):
    from agent import bestplan_sandbox as sandbox

    attempt_root = tmp_path / "attempt"
    source_dir = attempt_root / "source"
    runtime_dir = attempt_root / "runtime"
    scratch_dir = attempt_root / "scratch"
    control_dir = attempt_root / "control"
    for path in (source_dir / "allowed", runtime_dir, scratch_dir):
        path.mkdir(parents=True, exist_ok=True)
    for name in ("controller-a", "controller-b"):
        root = tmp_path / name
        (root / "agent").mkdir(parents=True)
        (root / "artifact.bin").write_bytes(name.encode())
        (root / "agent" / "bestplan_worker.py").write_text("# pinned worker\n")
    controller_python, runtime_paths = _fake_controller_runtime(tmp_path)
    monkeypatch.setattr(sandbox, "_macos_sandbox_available", lambda: True)

    def identity(
        controller: str,
        *,
        read_only: bool = False,
    ):
        return sandbox.candidate_sandbox_backend_identity(
            workspace=source_dir,
            allowed_paths=() if read_only else ("allowed/",),
            read_only=read_only,
            runtime_dir=runtime_dir,
            scratch_dir=scratch_dir,
            control_dir=control_dir,
            controller_source=tmp_path / controller,
            controller_python=controller_python,
            runtime_read_paths=runtime_paths,
            enabled_toolsets=("read_only_files",) if read_only else ("file",),
            expected_controller=_controller_identity(tmp_path / controller),
        )

    base = identity("controller-a")
    assert identity("controller-b")["policy_digest"] != base["policy_digest"]
    assert identity("controller-a", read_only=True)["policy_digest"] != base["policy_digest"]
    (runtime_paths[1] / "extension.so").write_bytes(b"mutated runtime")
    assert identity("controller-a")["policy_digest"] != base["policy_digest"]
    (tmp_path / "controller-a" / "artifact.bin").write_bytes(b"mutated")
    assert identity("controller-a")["policy_digest"] != base["policy_digest"]


def test_candidate_sandbox_rejects_controller_artifact_mismatch(tmp_path, monkeypatch):
    from agent import bestplan_sandbox as sandbox

    attempt = tmp_path / "attempt"
    source = attempt / "source"
    runtime = attempt / "runtime"
    scratch = attempt / "scratch"
    for path in (source / "allowed", runtime, scratch):
        path.mkdir(parents=True, exist_ok=True)
    controller = tmp_path / "controller"
    controller.mkdir()
    (controller / "worker.py").write_text("original\n")
    expected = _controller_identity(controller)
    (controller / "worker.py").write_text("substituted\n")
    interpreter, runtime_paths = _fake_controller_runtime(tmp_path)
    monkeypatch.setattr(sandbox, "_macos_sandbox_available", lambda: True)

    with pytest.raises(ValueError, match="differs from approval"):
        sandbox.candidate_sandbox_backend_identity(
            workspace=source,
            allowed_paths=("allowed/",),
            read_only=False,
            runtime_dir=runtime,
            scratch_dir=scratch,
            control_dir=attempt / "control",
            controller_source=controller,
            controller_python=interpreter,
            runtime_read_paths=runtime_paths,
            enabled_toolsets=("file",),
            expected_controller=expected,
        )


def test_pinned_candidate_runtime_includes_native_interpreter_library(tmp_path):
    from agent import bestplan_sandbox as sandbox

    interpreter, _runtime_paths = _fake_controller_runtime(tmp_path)
    native_library = interpreter.parent.parent / "lib" / "libpython3.11.dylib"
    native_library.write_bytes(b"pinned native runtime\n")

    pinned = sandbox.pinned_candidate_runtime_paths(interpreter)

    assert native_library.resolve() in pinned


def test_candidate_sandbox_binds_exact_bootstrap_and_seals_launch_descriptor(
    tmp_path, monkeypatch,
):
    from agent import bestplan_sandbox as sandbox

    attempt = tmp_path / "attempt"
    source = attempt / "source"
    runtime = attempt / "runtime"
    scratch = attempt / "scratch"
    controller = tmp_path / "controller"
    for path in (source / "allowed", runtime, scratch, controller / "agent"):
        path.mkdir(parents=True, exist_ok=True)
    (controller / "agent" / "bestplan_worker.py").write_text("# pinned worker\n")
    interpreter, runtime_paths = _fake_controller_runtime(tmp_path)
    monkeypatch.setattr(sandbox, "_macos_sandbox_available", lambda: True)
    expected = _controller_identity(controller)
    parent_channel, child_channel = socket.socketpair()
    launch = sandbox.create_bestplan_candidate_sandbox_launch(
        workspace=source,
        allowed_paths=("allowed/",),
        read_only=False,
        runtime_dir=runtime,
        scratch_dir=scratch,
        control_dir=attempt / "control",
        controller_source=controller,
        controller_python=interpreter,
        runtime_read_paths=runtime_paths,
        enabled_toolsets=("file",),
        expected_controller=expected,
        worker_environment=_candidate_environment(
            source, runtime, scratch, child_channel.fileno(),
        ),
        broker_fd=child_channel.fileno(),
    )
    try:
        command = launch.worker_command
        assert isinstance(command, tuple)
        assert command[:6] == (
            str(interpreter.resolve()), "-I", "-S", "-B", "-c",
            sandbox.CANDIDATE_BOOTSTRAP,
        )
        assert launch.bootstrap_sha256 == hashlib.sha256(
            sandbox.CANDIDATE_BOOTSTRAP.encode("utf-8")
        ).hexdigest()
        with pytest.raises(FrozenInstanceError):
            launch.broker_fd = parent_channel.fileno()
        with pytest.raises(TypeError):
            launch.worker_environment["PATH"] = "/substituted"

        original_digest = launch.policy_digest
        monkeypatch.setattr(
            sandbox, "CANDIDATE_BOOTSTRAP", sandbox.CANDIDATE_BOOTSTRAP + " ",
        )
        changed = sandbox.candidate_sandbox_backend_identity(
            workspace=source,
            allowed_paths=("allowed/",),
            read_only=False,
            runtime_dir=runtime,
            scratch_dir=scratch,
            control_dir=attempt / "control",
            controller_source=controller,
            controller_python=interpreter,
            runtime_read_paths=runtime_paths,
            enabled_toolsets=("file",),
            expected_controller=expected,
        )
        assert changed["policy_digest"] != original_digest
        assert launch.worker_command == command
    finally:
        launch.close()
        parent_channel.close()
        child_channel.close()


def test_candidate_sandbox_owns_broker_descriptor_and_launch_is_one_shot(
    tmp_path, monkeypatch,
):
    from agent import bestplan_sandbox as sandbox

    attempt = tmp_path / "attempt"
    source = attempt / "source"
    runtime = attempt / "runtime"
    scratch = attempt / "scratch"
    controller = tmp_path / "controller"
    for path in (source / "allowed", runtime, scratch, controller / "agent"):
        path.mkdir(parents=True, exist_ok=True)
    (controller / "agent" / "bestplan_worker.py").write_text("# pinned worker\n")
    interpreter, runtime_paths = _fake_controller_runtime(tmp_path)
    monkeypatch.setattr(sandbox, "_macos_sandbox_available", lambda: True)
    parent_channel, child_channel = socket.socketpair()
    caller_fd = child_channel.fileno()
    launch = sandbox.create_bestplan_candidate_sandbox_launch(
        workspace=source,
        allowed_paths=("allowed/",),
        read_only=False,
        runtime_dir=runtime,
        scratch_dir=scratch,
        control_dir=attempt / "control",
        controller_source=controller,
        controller_python=interpreter,
        runtime_read_paths=runtime_paths,
        enabled_toolsets=("file",),
        expected_controller=_controller_identity(controller),
        worker_environment=_candidate_environment(source, runtime, scratch, caller_fd),
        broker_fd=caller_fd,
    )
    child_channel.close()
    observed = {}

    def fake_popen(argv, **kwargs):
        observed["argv"] = tuple(argv)
        observed["kwargs"] = kwargs
        return SimpleNamespace(pid=1234)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    try:
        assert launch.broker_fd != caller_fd
        assert launch.worker_environment["HERMES_BESTPLAN_BROKER_FD"] == str(
            launch.broker_fd
        )
        launch.launch_worker()
        assert observed["kwargs"]["pass_fds"] == (launch.broker_fd,)
        with pytest.raises(RuntimeError, match="already launched"):
            launch.launch_worker()
    finally:
        launch.close()
        parent_channel.close()


def test_freeze_rejects_unleased_git_raw_alias_and_missing_artifact_paths(tmp_path):
    candidates = _candidates()
    snapshot = _snapshot(_repo(tmp_path / "repo"))
    spec = _spec(candidates)

    unleased = candidates.create_candidate_attempt(
        snapshot, plan_id=spec.plan_id, slice_id=spec.slice_id,
        attempts_root=tmp_path / "attempts", attempt_id="unleased",
    )
    (unleased.source_dir / "allowed" / "result.txt").write_text("ok\n")
    (unleased.source_dir / "outside.txt").write_text("changed\n")
    with pytest.raises(candidates.CandidateValidationError, match="write lease"):
        _freeze(candidates, snapshot, unleased, spec, raw_receipt={"status": "ok"})

    git_alias = candidates.create_candidate_attempt(
        snapshot, plan_id=spec.plan_id, slice_id=spec.slice_id,
        attempts_root=tmp_path / "attempts", attempt_id="git-alias",
    )
    (git_alias.source_dir / "allowed" / "result.txt").write_text("ok\n")
    (git_alias.source_dir / "allowed" / ".GiT").mkdir()
    with pytest.raises(candidates.CandidateValidationError, match="Git metadata"):
        _freeze(candidates, snapshot, git_alias, spec, raw_receipt={"status": "ok"})

    missing = candidates.create_candidate_attempt(
        snapshot, plan_id=spec.plan_id, slice_id=spec.slice_id,
        attempts_root=tmp_path / "attempts", attempt_id="missing",
    )
    with pytest.raises(candidates.CandidateValidationError, match="expected artifact"):
        _freeze(candidates, snapshot, missing, spec, raw_receipt={"status": "ok"})

    raw = candidates.create_candidate_attempt(
        snapshot, plan_id=spec.plan_id, slice_id=spec.slice_id,
        attempts_root=tmp_path / "attempts", attempt_id="raw-path",
    )
    (raw.source_dir / "allowed" / "result.txt").write_text("ok\n")
    raw_leaf = os.fsencode(raw.source_dir / "allowed") + b"/invalid-\xff"
    try:
        descriptor = os.open(raw_leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        # Darwin APFS rejects non-UTF-8 leaf creation before the scanner can
        # observe it. Exercise the same raw-path validator directly there.
        assert exc.errno == 92
        with pytest.raises(candidates.CandidateValidationError, match="UTF-8"):
            candidates.validate_raw_candidate_paths((b"allowed/invalid-\xff",))
    else:
        os.close(descriptor)
        with pytest.raises(candidates.CandidateValidationError, match="UTF-8"):
            _freeze(candidates, snapshot, raw, spec, raw_receipt={"status": "ok"})


def test_freeze_rejects_escaping_symlink_special_modes_and_hardlinks(tmp_path):
    candidates = _candidates()
    snapshot = _snapshot(_repo(tmp_path / "repo"))
    spec = _spec(candidates)

    symlink = candidates.create_candidate_attempt(
        snapshot, plan_id=spec.plan_id, slice_id=spec.slice_id,
        attempts_root=tmp_path / "attempts", attempt_id="symlink",
    )
    (symlink.source_dir / "allowed" / "result.txt").symlink_to("../../outside.txt")
    with pytest.raises(candidates.CandidateValidationError, match="symlink"):
        _freeze(candidates, snapshot, symlink, spec, raw_receipt={"status": "ok"})

    hardlink = candidates.create_candidate_attempt(
        snapshot, plan_id=spec.plan_id, slice_id=spec.slice_id,
        attempts_root=tmp_path / "attempts", attempt_id="hardlink",
    )
    result = hardlink.source_dir / "allowed" / "result.txt"
    result.write_text("ok\n")
    os.link(result, hardlink.source_dir / "allowed" / "second.txt")
    with pytest.raises(candidates.CandidateValidationError, match="hardlink"):
        _freeze(candidates, snapshot, hardlink, spec, raw_receipt={"status": "ok"})

    fifo = candidates.create_candidate_attempt(
        snapshot, plan_id=spec.plan_id, slice_id=spec.slice_id,
        attempts_root=tmp_path / "attempts", attempt_id="fifo",
    )
    (fifo.source_dir / "allowed" / "result.txt").write_text("ok\n")
    os.mkfifo(fifo.source_dir / "allowed" / "pipe")
    with pytest.raises(candidates.CandidateValidationError, match="special"):
        candidates.seal_candidate_attempt(fifo)


def test_seal_requires_two_identical_bounded_raw_scans(tmp_path, monkeypatch):
    candidates = _candidates()
    snapshot = _snapshot(_repo(tmp_path / "repo"))
    attempt = candidates.create_candidate_attempt(
        snapshot, plan_id="bp-plan-1", slice_id="code",
        attempts_root=tmp_path / "attempts", attempt_id="unstable",
    )
    original = candidates._scan_candidate_tree
    calls = 0

    def unstable(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        return result if calls == 1 else (*result[:-1], result[-1] + b"changed")

    monkeypatch.setattr(candidates, "_scan_candidate_tree", unstable)
    with pytest.raises(candidates.CandidateProofStale, match="stable"):
        candidates.seal_candidate_attempt(attempt)


def test_scanner_keeps_directory_descriptors_depth_bounded(tmp_path, monkeypatch):
    candidates = _candidates()
    attempt = candidates.create_candidate_attempt(
        _snapshot(_repo(tmp_path / "repo")),
        plan_id="bp-plan-1",
        slice_id="code",
        attempts_root=tmp_path / "attempts",
        attempt_id="wide-tree",
    )
    for index in range(32):
        (attempt.source_dir / "allowed" / f"dir-{index:02d}").mkdir()
    real_open = os.open
    real_close = os.close
    tracked: set[int] = set()

    def bounded_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        tracked.add(descriptor)
        if len(tracked) > 8:
            tracked.remove(descriptor)
            real_close(descriptor)
            raise OSError(24, "characterized descriptor limit")
        return descriptor

    def tracked_close(descriptor):
        tracked.discard(descriptor)
        return real_close(descriptor)

    monkeypatch.setattr(os, "open", bounded_open)
    monkeypatch.setattr(os, "close", tracked_close)
    candidates.seal_candidate_attempt(attempt)


def test_freeze_rejects_candidate_empty_directory_not_represented_by_git(tmp_path):
    candidates = _candidates()
    snapshot = _snapshot(_repo(tmp_path / "repo"))
    spec = _spec(candidates)
    attempt = candidates.create_candidate_attempt(
        snapshot,
        plan_id=spec.plan_id,
        slice_id=spec.slice_id,
        attempts_root=tmp_path / "attempts",
        attempt_id="empty-dir",
    )
    (attempt.source_dir / "allowed" / "result.txt").write_text("ok\n")
    (attempt.source_dir / "allowed" / "empty").mkdir()
    with pytest.raises(candidates.CandidateValidationError, match="empty directory"):
        _freeze(candidates, snapshot, attempt, spec, raw_receipt={"status": "ok"})


def test_freeze_uses_sealed_bytes_and_never_rereads_mutable_attempt_tree(tmp_path):
    candidates = _candidates()
    repo = _repo(tmp_path / "repo")
    snapshot = _snapshot(repo)
    spec = _spec(candidates)
    attempt = candidates.create_candidate_attempt(
        snapshot,
        plan_id=spec.plan_id,
        slice_id=spec.slice_id,
        attempts_root=tmp_path / "attempts",
        attempt_id="sealed-bytes",
    )
    result = attempt.source_dir / "allowed" / "result.txt"
    result.write_text("sealed\n")
    sealed = candidates.seal_candidate_attempt(attempt)
    result.write_text("mutated after seal\n")

    frozen = candidates._freeze_sealed_candidate_for_test(
        snapshot, sealed, spec, raw_receipt={"status": "completed"},
    )

    assert _git(repo, "show", f"{frozen.commit_oid}:allowed/result.txt") == "sealed"


def test_freeze_seeds_base_tree_and_hashes_only_changed_blobs(tmp_path, monkeypatch):
    candidates = _candidates()
    repo = _repo(tmp_path / "repo")
    snapshot = _snapshot(repo)
    spec = _spec(candidates)
    attempt = candidates.create_candidate_attempt(
        snapshot,
        plan_id=spec.plan_id,
        slice_id=spec.slice_id,
        attempts_root=tmp_path / "attempts",
        attempt_id="changed-only-index",
    )
    (attempt.source_dir / "allowed" / "result.txt").write_text("artifact\n")
    calls: list[str] = []
    original_git = candidates._git

    def observed_git(repo_identity, *args, **kwargs):
        calls.append(args[0])
        return original_git(repo_identity, *args, **kwargs)

    monkeypatch.setattr(candidates, "_git", observed_git)
    frozen = _freeze(
        candidates, snapshot, attempt, spec, raw_receipt={"status": "ok"},
    )

    assert calls.count("hash-object") == 1
    assert calls.count("read-tree") == 1
    assert calls.count("update-index") == 1
    assert calls.count("write-tree") == 1
    assert _git(repo, "show", f"{frozen.commit_oid}:allowed/result.txt") == "artifact"


def test_unchanged_candidate_reuses_captured_tree_without_hashing_blobs(tmp_path, monkeypatch):
    candidates = _candidates()
    repo = _repo(tmp_path / "repo")
    snapshot = _snapshot(repo)
    spec = _spec(
        candidates,
        allowed_paths=(),
        read_only=True,
        expected_artifacts=("allowed/base.txt",),
        toolsets=("read_only_files",),
    )
    attempt = candidates.create_candidate_attempt(
        snapshot,
        plan_id=spec.plan_id,
        slice_id=spec.slice_id,
        attempts_root=tmp_path / "attempts",
        attempt_id="unchanged-index",
    )
    hash_calls = 0
    original_git = candidates._git

    def observed_git(repo_identity, *args, **kwargs):
        nonlocal hash_calls
        if args and args[0] == "hash-object":
            hash_calls += 1
        return original_git(repo_identity, *args, **kwargs)

    monkeypatch.setattr(candidates, "_git", observed_git)
    frozen = _freeze(
        candidates, snapshot, attempt, spec, raw_receipt={"status": "ok"},
    )

    assert hash_calls == 0
    assert frozen.tree_oid == snapshot.tree_oid


def test_temp_index_batch_preserves_raw_utf8_path_and_deletion(tmp_path, monkeypatch):
    candidates = _candidates()
    repo = _repo(tmp_path / "repo")
    snapshot = _snapshot(repo)
    spec = _spec(
        candidates,
        allowed_paths=("allowed/",),
        expected_artifacts=("allowed/ñ.txt",),
    )
    attempt = candidates.create_candidate_attempt(
        snapshot,
        plan_id=spec.plan_id,
        slice_id=spec.slice_id,
        attempts_root=tmp_path / "attempts",
        attempt_id="raw-index-batch",
    )
    (attempt.source_dir / "allowed" / "base.txt").unlink()
    (attempt.source_dir / "allowed" / "ñ.txt").write_text("raw path\n")
    index_inputs: list[bytes] = []
    original_git = candidates._git

    def observed_git(repo_identity, *args, **kwargs):
        if args and args[0] == "update-index":
            index_inputs.append(kwargs.get("input_bytes", b""))
        return original_git(repo_identity, *args, **kwargs)

    monkeypatch.setattr(candidates, "_git", observed_git)
    frozen = _freeze(
        candidates, snapshot, attempt, spec, raw_receipt={"status": "ok"},
    )

    assert len(index_inputs) == 1
    assert b"allowed/base.txt\0" in index_inputs[0]
    assert "allowed/ñ.txt".encode() + b"\0" in index_inputs[0]
    tree_paths = _git(repo, "ls-tree", "-rz", "--name-only", frozen.tree_oid)
    assert "allowed/base.txt" not in tree_paths
    assert "allowed/ñ.txt" in tree_paths


def test_freeze_absolute_deadline_precedes_git_and_candidate_ref_creation(tmp_path):
    candidates = _candidates()
    repo = _repo(tmp_path / "repo")
    snapshot = _snapshot(repo)
    spec = _spec(candidates)
    attempt = candidates.create_candidate_attempt(
        snapshot,
        plan_id=spec.plan_id,
        slice_id=spec.slice_id,
        attempts_root=tmp_path / "attempts",
        attempt_id="expired-freeze",
    )
    (attempt.source_dir / "allowed" / "result.txt").write_text("artifact\n")
    sealed = candidates.seal_candidate_attempt(attempt)
    deadline = time.monotonic() - 1

    with pytest.raises(candidates.CandidateExecutionError, match="deadline"):
        candidates._freeze_sealed_candidate_for_test(
            snapshot,
            sealed,
            spec,
            raw_receipt={"status": "ok"},
            deadline=deadline,
        )
    with pytest.raises(candidates.CandidateExecutionError, match="deadline"):
        candidates._git(snapshot.repo, "status", "--porcelain", deadline=deadline)
    assert not _git(
        repo, "for-each-ref", "--format=%(refname)", attempt.ref_name,
    )


def test_frozen_receipt_is_deeply_immutable_and_digest_bound(tmp_path):
    candidates = _candidates()
    snapshot = _snapshot(_repo(tmp_path / "repo"))
    spec = _spec(candidates)
    attempt = candidates.create_candidate_attempt(
        snapshot,
        plan_id=spec.plan_id,
        slice_id=spec.slice_id,
        attempts_root=tmp_path / "attempts",
        attempt_id="immutable-receipt",
    )
    (attempt.source_dir / "allowed" / "result.txt").write_text("ok\n")
    receipt = {"status": "completed", "nested": {"items": ["first"]}}

    frozen = _freeze(
        candidates, snapshot, attempt, spec, raw_receipt=receipt,
    )

    with pytest.raises(AttributeError):
        frozen.raw_receipt["nested"]["items"].append("mutated")
    canonical = json.dumps(
        receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()
    assert frozen.raw_receipt_sha256 == hashlib.sha256(canonical).hexdigest()
    assert frozen.raw_receipt["nested"]["items"] == ("first",)

def test_host_freeze_is_deterministic_parented_to_base_and_cas_anchors_refs(tmp_path):
    candidates = _candidates()
    repo = _repo(tmp_path / "repo")
    snapshot = _snapshot(repo)
    spec = _spec(candidates)
    frozen = []
    for attempt_id in ("deterministic-a", "deterministic-b"):
        attempt = candidates.create_candidate_attempt(
            snapshot, plan_id=spec.plan_id, slice_id=spec.slice_id,
            attempts_root=tmp_path / "attempts", attempt_id=attempt_id,
        )
        (attempt.source_dir / "allowed" / "base.txt").write_text("changed\n")
        (attempt.source_dir / "allowed" / "result.txt").write_text("artifact\n")
        frozen.append(
            _freeze(
                candidates, snapshot, attempt, spec,
                raw_receipt={"status": "completed"},
            )
        )

    assert frozen[0].tree_oid == frozen[1].tree_oid
    assert frozen[0].commit_oid == frozen[1].commit_oid
    assert frozen[0].ref_name != frozen[1].ref_name
    assert _git(repo, "rev-parse", f"{frozen[0].commit_oid}^") == snapshot.head_oid
    assert _git(repo, "rev-parse", f"{frozen[0].commit_oid}^{{tree}}") == frozen[0].tree_oid
    assert _git(repo, "rev-parse", frozen[0].ref_name) == frozen[0].commit_oid
    assert frozen[0].changed_paths == (b"allowed/base.txt", b"allowed/result.txt")


def test_freeze_ref_cas_never_replaces_an_existing_attempt_ref(tmp_path):
    candidates = _candidates()
    repo = _repo(tmp_path / "repo")
    snapshot = _snapshot(repo)
    spec = _spec(candidates)
    attempt = candidates.create_candidate_attempt(
        snapshot, plan_id=spec.plan_id, slice_id=spec.slice_id,
        attempts_root=tmp_path / "attempts", attempt_id="cas",
    )
    (attempt.source_dir / "allowed" / "result.txt").write_text("artifact\n")
    _git(repo, "update-ref", attempt.ref_name, snapshot.head_oid)
    with pytest.raises(candidates.CandidateRefConflict, match="different commit"):
        _freeze(candidates, snapshot, attempt, spec, raw_receipt={"status": "ok"})
    assert _git(repo, "rev-parse", attempt.ref_name) == snapshot.head_oid


def test_candidate_ref_anchor_is_idempotent_only_for_the_same_commit(tmp_path):
    candidates = _candidates()
    repo = _repo(tmp_path / "repo")
    snapshot = _snapshot(repo)
    ref_name = candidates.candidate_ref_name("bp-plan-1", "code", "retry")
    assert candidates._anchor_candidate_ref(snapshot.repo, ref_name, snapshot.head_oid) == snapshot.head_oid
    assert candidates._anchor_candidate_ref(snapshot.repo, ref_name, snapshot.head_oid) == snapshot.head_oid
    (repo / "allowed" / "base.txt").write_text("different\n")
    _git(repo, "add", "allowed/base.txt")
    _git(repo, "commit", "-qm", "different")
    different = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(candidates.CandidateRefConflict, match="different"):
        candidates._anchor_candidate_ref(snapshot.repo, ref_name, different)
    assert _git(repo, "rev-parse", ref_name) == snapshot.head_oid


def test_trusted_candidate_authority_surfaces_are_not_publicly_injectable():
    candidates = _candidates()
    public_parameters = inspect.signature(candidates.run_and_freeze_candidate).parameters
    assert "sandbox_factory" not in public_parameters
    assert "process_identity_resolver" not in public_parameters
    assert "process_group_reaper" not in public_parameters
    assert not hasattr(candidates, "anchor_candidate_ref")
    assert not hasattr(candidates, "freeze_sealed_candidate")


def test_runner_rejects_controller_from_a_different_repository_before_launch(tmp_path):
    candidates = _candidates()
    snapshot = _snapshot(_repo(tmp_path / "repo"))
    controller = tmp_path / "controller-n-1"
    worker_script = controller / "agent" / "bestplan_worker.py"
    worker_script.parent.mkdir(parents=True)
    worker_script.write_text("# retained controller worker\n")

    with pytest.raises(candidates.CandidateValidationError, match="controller repository"):
        candidates.run_and_freeze_candidate(
            snapshot=snapshot,
            spec=_spec(candidates),
            attempts_root=tmp_path / "attempts",
            controller_source=controller,
            controller_python=Path(sys.executable),
            runtime_read_paths=(),
            expected_controller=_controller_identity(
                controller, repository_id="different-repository",
            ),
            authority_client=object(),
            timeout_seconds=2,
            attempt_id="cross-repository",
        )


def test_runner_rejects_broad_controller_source_inside_primary_repository(tmp_path):
    candidates = _candidates()
    repo = _repo(tmp_path / "repo")
    controller = repo / "controller-n-1"
    worker_script = controller / "agent" / "bestplan_worker.py"
    worker_script.parent.mkdir(parents=True)
    worker_script.write_text("# retained controller worker\n")
    snapshot = _snapshot(repo)

    with pytest.raises(candidates.CandidateValidationError, match="controller source overlap"):
        candidates.run_and_freeze_candidate(
            snapshot=snapshot,
            spec=_spec(candidates),
            attempts_root=tmp_path / "attempts",
            controller_source=controller,
            controller_python=Path(sys.executable),
            runtime_read_paths=(),
            expected_controller=_controller_identity(
                controller, repository_id=snapshot.repo.repository_id,
            ),
            authority_client=object(),
            timeout_seconds=2,
            attempt_id="controller-overlap",
        )


def test_host_broker_requires_exact_schema_and_response_subset_per_turn():
    candidates = _candidates()
    spec = _spec(candidates)
    accounting = candidates._BrokerAccounting()
    altered = _write_file_schema()
    altered["function"] = dict(altered["function"])
    altered["function"]["description"] = "substituted schema"
    with pytest.raises(candidates.CandidateExecutionError, match="tool schema"):
        candidates._validate_host_request({
            "request_id": "turn-00000001",
            "max_output_tokens": 128,
            "request": {
                "model": spec.model,
                "messages": [{"role": "user", "content": "edit"}],
                "tools": [altered],
                "tool_choice": "auto",
                "max_tokens": 128,
                "stream": False,
            },
        }, spec, accounting)

    request = candidates._validate_host_request({
        "request_id": "turn-00000001",
        "max_output_tokens": 128,
        "request": {
            "model": spec.model,
            "messages": [{"role": "user", "content": "summarize"}],
            "tools": [],
            "max_tokens": 128,
            "stream": False,
        },
    }, spec, candidates._BrokerAccounting())
    from agent.bestplan_authority_client import BrokerTurnResponse

    response_body = {
        "id": "chatcmpl-unadvertised",
        "object": "chat.completion",
        "created": 1,
        "model": spec.model,
        "choices": [{
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": "{}"},
                }],
            },
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    response = BrokerTurnResponse(
        request_id=request.request_id,
        response_json=json.dumps(response_body, sort_keys=True, separators=(",", ":")),
        input_tokens=1,
        output_tokens=1,
    )
    with pytest.raises(candidates.CandidateExecutionError, match="tool response"):
        candidates._validate_host_response(
            response, request, spec, candidates._BrokerAccounting(),
        )


def test_host_response_usage_must_exactly_match_authority_accounting():
    candidates = _candidates()
    spec = _spec(candidates)
    accounting = candidates._BrokerAccounting()
    request = candidates._validate_host_request({
        "request_id": "turn-00000001",
        "max_output_tokens": 4,
        "request": {
            "model": spec.model,
            "messages": [{"role": "user", "content": "summarize"}],
            "tools": [],
            "max_tokens": 4,
            "stream": False,
        },
    }, spec, accounting)
    body = {
        "id": "chatcmpl-usage-mismatch",
        "object": "chat.completion",
        "created": 1,
        "model": spec.model,
        "choices": [{
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "done"},
        }],
        "usage": {"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10},
    }
    from agent.bestplan_authority_client import BrokerTurnResponse

    response = BrokerTurnResponse(
        request_id=request.request_id,
        response_json=json.dumps(body, sort_keys=True, separators=(",", ":")),
        input_tokens=1,
        output_tokens=1,
    )
    with pytest.raises(candidates.CandidateExecutionError, match="usage"):
        candidates._validate_host_response(response, request, spec, accounting)
    assert accounting.input_tokens == 0
    assert accounting.output_tokens == 0


def test_host_broker_bounds_aggregate_tool_calls_across_responses(monkeypatch):
    candidates = _candidates()
    spec = _spec(candidates)
    accounting = candidates._BrokerAccounting()
    monkeypatch.setattr(
        candidates, "MAX_BROKER_TOOL_CALLS_PER_ATTEMPT", 1, raising=False,
    )
    from agent.bestplan_authority_client import BrokerTurnResponse

    for number in (1, 2):
        request = candidates._validate_host_request({
            "request_id": f"turn-{number:08d}",
            "max_output_tokens": 4,
            "request": {
                "model": spec.model,
                "messages": [{"role": "user", "content": "edit"}],
                "tools": [_write_file_schema()],
                "tool_choice": "auto",
                "max_tokens": 4,
                "stream": False,
            },
        }, spec, accounting)
        body = {
            "id": f"chatcmpl-tool-{number}",
            "object": "chat.completion",
            "created": number,
            "model": spec.model,
            "choices": [{
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": f"call-{number}",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps(
                                {"path": "allowed/result.txt", "content": "ok\n"},
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        },
                    }],
                },
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        response = BrokerTurnResponse(
            request_id=request.request_id,
            response_json=json.dumps(body, sort_keys=True, separators=(",", ":")),
            input_tokens=1,
            output_tokens=1,
        )
        if number == 1:
            candidates._validate_host_response(response, request, spec, accounting)
        else:
            with pytest.raises(candidates.CandidateExecutionError, match="aggregate tool"):
                candidates._validate_host_response(response, request, spec, accounting)


def test_failed_post_write_proof_removes_exact_candidate_ref(tmp_path, monkeypatch):
    candidates = _candidates()
    repo = _repo(tmp_path / "repo")
    snapshot = _snapshot(repo)
    spec = _spec(candidates)
    attempt = candidates.create_candidate_attempt(
        snapshot,
        plan_id=spec.plan_id,
        slice_id=spec.slice_id,
        attempts_root=tmp_path / "attempts",
        attempt_id="post-proof",
    )
    (attempt.source_dir / "allowed" / "result.txt").write_text("ok\n")
    monkeypatch.setattr(
        candidates,
        "_verify_frozen_git_objects",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            candidates.CandidateExecutionError("post-write proof failed")
        ),
    )
    with pytest.raises(candidates.CandidateExecutionError, match="post-write proof"):
        _freeze(candidates, snapshot, attempt, spec, raw_receipt={"status": "ok"})
    assert not _git(
        repo, "for-each-ref", "--format=%(refname)", attempt.ref_name,
    )


def test_failed_base_ref_cleanup_removes_unreturned_candidate_and_base_refs(
    tmp_path, monkeypatch,
):
    candidates = _candidates()
    repo = _repo(tmp_path / "repo")
    snapshot = _snapshot(repo)
    spec = _spec(candidates)
    attempt = candidates.create_candidate_attempt(
        snapshot,
        plan_id=spec.plan_id,
        slice_id=spec.slice_id,
        attempts_root=tmp_path / "attempts",
        attempt_id="base-cleanup-failure",
    )
    (attempt.source_dir / "allowed" / "result.txt").write_text("ok\n")
    original_delete = candidates._delete_ref
    failed_once = False

    def fail_first_base_delete(repo_identity, ref_name, expected_oid, **kwargs):
        nonlocal failed_once
        if ref_name == attempt.base_ref_name and not failed_once:
            failed_once = True
            raise candidates.CandidateExecutionError("injected base cleanup failure")
        return original_delete(repo_identity, ref_name, expected_oid, **kwargs)

    monkeypatch.setattr(candidates, "_delete_ref", fail_first_base_delete)

    with pytest.raises(candidates.CandidateExecutionError, match="cleanup"):
        _freeze(candidates, snapshot, attempt, spec, raw_receipt={"status": "ok"})

    assert failed_once is True
    assert not _git(
        repo,
        "for-each-ref",
        "--format=%(refname)",
        attempt.ref_name,
        attempt.base_ref_name,
    )


def test_freeze_rejects_repository_indirection_retarget_before_git_writes(tmp_path):
    candidates = _candidates()
    repo = _repo(tmp_path / "repo")
    other = _repo(tmp_path / "other")
    snapshot = _snapshot(repo)
    spec = _spec(candidates)
    attempt = candidates.create_candidate_attempt(
        snapshot,
        plan_id=spec.plan_id,
        slice_id=spec.slice_id,
        attempts_root=tmp_path / "attempts",
        attempt_id="repo-retarget",
    )
    (attempt.source_dir / "allowed" / "result.txt").write_text("ok\n")
    sealed = candidates.seal_candidate_attempt(attempt)
    original_git = repo / ".git-original"
    (repo / ".git").rename(original_git)
    (repo / ".git").symlink_to(other / ".git", target_is_directory=True)

    with pytest.raises(candidates.CandidateValidationError, match="repository identity"):
        candidates._freeze_sealed_candidate_for_test(
            snapshot, sealed, spec, raw_receipt={"status": "ok"},
        )
    assert not _git(
        other, "for-each-ref", "--format=%(refname)", "refs/hermes-bestplan",
    )


def test_host_commit_and_ref_creation_ignore_repo_hooks_filters_and_identity_config(
    tmp_path,
):
    candidates = _candidates()
    repo = _repo(tmp_path / "repo")
    snapshot = _snapshot(repo)
    hooks = tmp_path / "hostile-hooks"
    hooks.mkdir()
    marker = tmp_path / "hook-ran"
    hook = hooks / "reference-transaction"
    hook.write_text(f"#!/bin/sh\nprintf ran > {str(marker)!r}\nexit 91\n")
    hook.chmod(0o755)
    _git(repo, "config", "core.hooksPath", str(hooks))
    _git(repo, "config", "filter.hostile.clean", f"touch {marker}")
    _git(repo, "config", "user.name", "Ambient Name")
    spec = _spec(candidates)
    attempt = candidates.create_candidate_attempt(
        snapshot, plan_id=spec.plan_id, slice_id=spec.slice_id,
        attempts_root=tmp_path / "attempts", attempt_id="config-neutral",
    )
    (attempt.source_dir / "allowed" / "result.txt").write_text("artifact\n")
    frozen = _freeze(
        candidates, snapshot, attempt, spec, raw_receipt={"status": "ok"},
    )
    assert _git(repo, "rev-parse", frozen.ref_name) == frozen.commit_oid
    assert not marker.exists()


class _FakeAuthority:
    def __init__(self, events: list[object]):
        self.events = events
        self.capability = None

    def register_model_attempt(
        self, attempt_id, worker_identity, model, request_budget, token_budget, expires_at,
    ):
        from agent.bestplan_authority_client import BrokerCapability

        self.events.append((
            "register", attempt_id, worker_identity, model,
            request_budget, token_budget, expires_at,
        ))
        self.capability = BrokerCapability(
            attempt_id, worker_identity, "opaque-capability-must-stay-parent-side",
        )
        return self.capability

    def model_request(self, capability, request):
        from agent.bestplan_authority_client import BrokerTurnResponse

        assert capability is self.capability
        self.events.append(("model_request", request.request_id, request.request_json))
        response = {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": 1,
            "model": "test/model",
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "done"},
            }],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        }
        return BrokerTurnResponse(
            request_id=request.request_id,
            response_json=json.dumps(response, sort_keys=True, separators=(",", ":")),
            input_tokens=2,
            output_tokens=1,
        )

    def revoke_model_attempt(self, capability):
        assert capability is self.capability
        self.events.append(("revoke", capability.attempt_id))


class _FakeProcess:
    def __init__(self, workspace: Path, broker_fd: int, *, timeout: bool = False):
        self.pid = 4242
        self.returncode = None
        self.workspace = workspace
        self.broker_fd = os.dup(broker_fd)
        self.timeout = timeout
        self.payload = None

    def communicate(self, value, timeout=None):
        self.payload = json.loads(value)
        if self.timeout:
            raise subprocess.TimeoutExpired(["worker"], timeout)
        channel = socket.socket(fileno=self.broker_fd)
        _send_frame(channel, {
            "max_output_tokens": 128,
            "request": {
                "model": self.payload["runtime"]["model"],
                "messages": [{"role": "user", "content": "complete"}],
                "tools": [_write_file_schema()],
                "tool_choice": "auto",
                "max_tokens": 128,
                "stream": False,
            },
            "request_id": "turn-00000001",
        })
        response = _receive_frame(channel)
        assert response["ok"] is True
        assert response["request_id"] == "turn-00000001"
        (self.workspace / "allowed" / "result.txt").write_text("artifact\n")
        self.returncode = 0
        self.channel = channel
        output = {
            "status": "completed",
            "summary": "candidate complete",
            "error": None,
            "api_calls": 999,
        }
        return "HERMES_BESTPLAN_RESULT=" + json.dumps(output), ""

    def poll(self):
        return self.returncode


def _observe_broker_close_before_reap(process, events):
    channel = getattr(process, "channel", None)
    if channel is None:
        channel = socket.socket(fileno=process.broker_fd)
    channel.settimeout(1)
    assert channel.recv(1) == b""
    events.append(("broker_closed", process.pid))
    channel.close()
    process.returncode = 0
    events.append(("reap", process.pid))


class _FakeLaunch:
    policy_digest = "candidate-policy"

    def __init__(self, launch_kwargs: dict, *, timeout: bool = False, events=None):
        candidates = _candidates()
        self.workspace = Path(launch_kwargs["workspace"])
        self.timeout = timeout
        controller = Path(launch_kwargs["controller_source"]).resolve()
        self.argv = [
            str(Path(launch_kwargs["controller_python"]).resolve()),
            "-I", "-S", "-B", "-c", candidates.CANDIDATE_BOOTSTRAP,
            str(controller), str((controller / "agent" / "bestplan_worker.py").resolve()),
            *[str(Path(path).resolve()) for path in launch_kwargs["runtime_read_paths"]],
        ]
        self.kwargs = {
            "env": dict(launch_kwargs["worker_environment"]),
            "pass_fds": (launch_kwargs["broker_fd"],),
            "start_new_session": True,
        }
        self.process = None
        self.closed = False
        self.events = events

    def launch_worker(self):
        self.process = _FakeProcess(
            self.workspace, self.kwargs["pass_fds"][0], timeout=self.timeout,
        )
        return self.process

    def verify_identity(self, *, deadline=None):
        if self.events is not None:
            self.events.append((
                "verify_identity", self.process.pid if self.process else None, deadline,
            ))

    def close(self):
        self.closed = True


_PIPE_WORKER_CODE = r'''
import json, pathlib, socket, struct, sys

def receive_exact(channel, size):
    output = bytearray()
    while len(output) < size:
        chunk = channel.recv(size - len(output))
        if not chunk:
            raise SystemExit(91)
        output.extend(chunk)
    return bytes(output)

workspace = pathlib.Path(sys.argv[1])
channel = socket.socket(fileno=int(sys.argv[2]))
payload = json.loads(sys.stdin.read())
request = {
    "max_output_tokens": 128,
    "request": {
        "max_tokens": 128,
        "messages": [{"content": "finish", "role": "user"}],
        "model": payload["runtime"]["model"],
        "stream": False,
        "tools": [],
    },
    "request_id": "turn-00000001",
}
raw = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
channel.sendall(struct.pack("!I", len(raw)) + raw)
size = struct.unpack("!I", receive_exact(channel, 4))[0]
response = json.loads(receive_exact(channel, size))
if response.get("ok") is not True:
    raise SystemExit(92)
(workspace / "allowed" / "result.txt").write_text("artifact\n")
result = {
    "api_calls": 1,
    "error": None,
    "status": "completed",
    "summary": "candidate complete",
}
sys.stdout.write("HERMES_BESTPLAN_RESULT=" + json.dumps(result, sort_keys=True))
sys.stdout.flush()
while channel.recv(1):
    pass
'''


class _PipeLaunch:
    policy_digest = "candidate-policy"

    def __init__(self, launch_kwargs: dict, events: list[object]):
        self.workspace = Path(launch_kwargs["workspace"])
        self.broker_fd = launch_kwargs["broker_fd"]
        self.events = events
        self.process = None

    def launch_worker(self):
        self.process = subprocess.Popen(
            [sys.executable, "-c", _PIPE_WORKER_CODE, str(self.workspace), str(self.broker_fd)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            pass_fds=(self.broker_fd,),
            start_new_session=True,
        )
        return self.process

    def verify_identity(self, *, deadline=None):
        self.events.append(("verify_identity", self.process.pid, deadline))

    def close(self):
        self.events.append(("launch_close", None))


def test_runner_closes_broker_before_waiting_for_pipe_worker_exit(tmp_path):
    candidates = _candidates()
    snapshot = _snapshot(_repo(tmp_path / "repo"))
    controller = tmp_path / "controller-n-1"
    (controller / "agent").mkdir(parents=True)
    (controller / "agent" / "bestplan_worker.py").write_text("# retained worker\n")
    events: list[object] = []
    authority = _FakeAuthority(events)
    expected_digest = hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest()

    def identity(pid, _executable):
        from agent.bestplan_authority_client import WorkerIdentity

        return WorkerIdentity(pid, os.getuid(), f"pipe-start:{pid}", expected_digest)

    def reaper(process, **kwargs):
        events.append(("reap", process.pid))
        candidates.terminate_process_group(process, **kwargs)

    frozen = candidates._run_and_freeze_candidate_for_test(
        snapshot=snapshot,
        spec=_spec(candidates),
        attempts_root=tmp_path / "attempts",
        controller_source=controller,
        controller_python=Path(sys.executable),
        runtime_read_paths=(),
        expected_controller=_controller_identity(
            controller, repository_id=snapshot.repo.repository_id,
        ),
        authority_client=authority,
        timeout_seconds=15,
        attempt_id="pipe-close-order",
        sandbox_factory=lambda **kwargs: _PipeLaunch(kwargs, events),
        process_identity_resolver=identity,
        process_group_reaper=reaper,
    )

    names = [event[0] for event in events]
    assert names.index("model_request") < names.index("reap")
    assert names.index("reap") < names.index("revoke")
    assert names.index("revoke") < names.index("verify_identity")
    assert frozen.commit_oid


class _PostAdmissionPollGuard:
    def __init__(self, process, authority):
        self._process = process
        self._authority = authority

    def __getattr__(self, name):
        return getattr(self._process, name)

    def poll(self):
        if self._authority.capability is not None:
            raise AssertionError("waitpid/poll after model admission")
        return self._process.poll()


def test_runner_does_not_poll_or_waitpid_after_model_admission_before_broker_close(
    tmp_path, monkeypatch,
):
    candidates = _candidates()
    snapshot = _snapshot(_repo(tmp_path / "repo"))
    controller = tmp_path / "controller-n-1"
    (controller / "agent").mkdir(parents=True)
    (controller / "agent" / "bestplan_worker.py").write_text("# retained worker\n")
    events: list[object] = []
    authority = _FakeAuthority(events)
    expected_digest = hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest()

    class GuardedLaunch(_PipeLaunch):
        def launch_worker(self):
            real_process = super().launch_worker()
            self.real_process = real_process
            self.process = _PostAdmissionPollGuard(real_process, authority)
            return self.process

    original_close = candidates._close_broker_channel

    def observed_close(channel):
        events.append(("broker_close", None))
        return original_close(channel)

    monkeypatch.setattr(candidates, "_close_broker_channel", observed_close)

    def identity(pid, _executable):
        from agent.bestplan_authority_client import WorkerIdentity

        return WorkerIdentity(pid, os.getuid(), f"guarded-start:{pid}", expected_digest)

    def reaper(process, **kwargs):
        events.append(("reap", process.pid))
        candidates.terminate_process_group(process._process, **kwargs)

    frozen = candidates._run_and_freeze_candidate_for_test(
        snapshot=snapshot,
        spec=_spec(candidates),
        attempts_root=tmp_path / "attempts",
        controller_source=controller,
        controller_python=Path(sys.executable),
        runtime_read_paths=(),
        expected_controller=_controller_identity(
            controller, repository_id=snapshot.repo.repository_id,
        ),
        authority_client=authority,
        timeout_seconds=5,
        attempt_id="no-post-admission-poll",
        sandbox_factory=lambda **kwargs: GuardedLaunch(kwargs, events),
        process_identity_resolver=identity,
        process_group_reaper=reaper,
    )

    names = [event[0] for event in events]
    assert names.index("broker_close") < names.index("reap") < names.index("revoke")
    assert frozen.commit_oid


_MALFORMED_PIPE_WORKER_CODE = r'''
import json, socket, struct, sys

def receive_exact(channel, size):
    output = bytearray()
    while len(output) < size:
        chunk = channel.recv(size - len(output))
        if not chunk:
            raise SystemExit(91)
        output.extend(chunk)
    return bytes(output)

channel = socket.socket(fileno=int(sys.argv[1]))
payload = json.loads(sys.stdin.read())
request = {
    "max_output_tokens": 128,
    "request": {
        "max_tokens": 128,
        "messages": [{"content": "finish", "role": "user"}],
        "model": payload["runtime"]["model"],
        "stream": False,
        "tools": [],
    },
    "request_id": "turn-00000001",
}
raw = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
channel.sendall(struct.pack("!I", len(raw)) + raw)
size = struct.unpack("!I", receive_exact(channel, 4))[0]
response = json.loads(receive_exact(channel, size))
if response.get("ok") is not True:
    raise SystemExit(92)
sys.stdout.write("HERMES_BESTPLAN_RESULT={\n")
sys.stdout.flush()
while channel.recv(1):
    pass
'''


class _MalformedPipeLaunch(_PipeLaunch):
    def launch_worker(self):
        self.process = subprocess.Popen(
            [sys.executable, "-c", _MALFORMED_PIPE_WORKER_CODE, str(self.broker_fd)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            pass_fds=(self.broker_fd,),
            start_new_session=True,
        )
        return self.process


def test_complete_malformed_result_frame_closes_broker_without_consuming_deadline(tmp_path):
    candidates = _candidates()
    snapshot = _snapshot(_repo(tmp_path / "repo"))
    controller = tmp_path / "controller-n-1"
    (controller / "agent").mkdir(parents=True)
    (controller / "agent" / "bestplan_worker.py").write_text("# retained worker\n")
    events: list[object] = []
    authority = _FakeAuthority(events)
    expected_digest = hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest()

    def identity(pid, _executable):
        from agent.bestplan_authority_client import WorkerIdentity

        return WorkerIdentity(pid, os.getuid(), f"malformed-start:{pid}", expected_digest)

    started = time.monotonic()
    with pytest.raises(candidates.CandidateExecutionError, match="worker result"):
        candidates._run_and_freeze_candidate_for_test(
            snapshot=snapshot,
            spec=_spec(candidates),
            attempts_root=tmp_path / "attempts",
            controller_source=controller,
            controller_python=Path(sys.executable),
            runtime_read_paths=(),
            expected_controller=_controller_identity(
                controller, repository_id=snapshot.repo.repository_id,
            ),
            authority_client=authority,
            timeout_seconds=2,
            attempt_id="malformed-result",
            sandbox_factory=lambda **kwargs: _MalformedPipeLaunch(kwargs, events),
            process_identity_resolver=identity,
            process_group_reaper=candidates.terminate_process_group,
        )
    assert time.monotonic() - started < 2


def test_blocked_stdin_writer_is_reaped_before_pipe_close(tmp_path, monkeypatch):
    candidates = _candidates()
    snapshot = _snapshot(_repo(tmp_path / "repo"))
    controller = tmp_path / "controller-n-1"
    (controller / "agent").mkdir(parents=True)
    (controller / "agent" / "bestplan_worker.py").write_text("# retained worker\n")
    events: list[object] = []
    authority = _FakeAuthority(events)
    expected_digest = hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest()
    original_close_input = candidates._BoundedProcessCapture.close_input

    def observed_close_input(capture):
        events.append(("input_close", capture.process.pid))
        return original_close_input(capture)

    monkeypatch.setattr(
        candidates._BoundedProcessCapture, "close_input", observed_close_input,
    )

    class NoReadLaunch(_PipeLaunch):
        def launch_worker(self):
            code = (
                "import sys,time; "
                "sys.stdout.write('HERMES_BESTPLAN_RESULT={\\n'); sys.stdout.flush(); "
                "time.sleep(5)"
            )
            self.process = subprocess.Popen(
                [sys.executable, "-c", code],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            return self.process

    def identity(pid, _executable):
        from agent.bestplan_authority_client import WorkerIdentity

        return WorkerIdentity(pid, os.getuid(), f"blocked-input-start:{pid}", expected_digest)

    def reaper(process, **kwargs):
        events.append(("reap", process.pid))
        candidates.terminate_process_group(process, **kwargs)

    started = time.monotonic()
    with pytest.raises(candidates.CandidateExecutionError):
        candidates._run_and_freeze_candidate_for_test(
            snapshot=snapshot,
            spec=_spec(candidates, goal="x" * 200_000),
            attempts_root=tmp_path / "attempts",
            controller_source=controller,
            controller_python=Path(sys.executable),
            runtime_read_paths=(),
            expected_controller=_controller_identity(
                controller, repository_id=snapshot.repo.repository_id,
            ),
            authority_client=authority,
            timeout_seconds=5,
            attempt_id="blocked-input",
            sandbox_factory=lambda **kwargs: NoReadLaunch(kwargs, events),
            process_identity_resolver=identity,
            process_group_reaper=reaper,
        )
    assert time.monotonic() - started < 8
    names = [event[0] for event in events]
    assert "input_close" in names, events
    assert names.index("reap") < names.index("input_close"), events


def test_output_overflow_closes_admission_and_reaps_without_waiting_for_timeout(tmp_path):
    candidates = _candidates()
    snapshot = _snapshot(_repo(tmp_path / "repo"))
    controller = tmp_path / "controller-n-1"
    (controller / "agent").mkdir(parents=True)
    (controller / "agent" / "bestplan_worker.py").write_text("# retained worker\n")
    events: list[object] = []
    authority = _FakeAuthority(events)
    expected_digest = hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest()

    class OverflowLaunch(_PipeLaunch):
        def launch_worker(self):
            code = (
                "import sys,time; sys.stdin.read(); "
                "sys.stdout.write('HERMES_BESTPLAN_RESULT={\\\"summary\\\":\\\"'); "
                f"sys.stdout.write('x'*{2 * 1024 * 1024 + 1}); sys.stdout.flush(); "
                "time.sleep(30)"
            )
            self.process = subprocess.Popen(
                [sys.executable, "-c", code],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            return self.process

    def identity(pid, _executable):
        from agent.bestplan_authority_client import WorkerIdentity

        return WorkerIdentity(pid, os.getuid(), f"overflow-start:{pid}", expected_digest)

    started = time.monotonic()
    with pytest.raises(candidates.CandidateExecutionError, match="output limit"):
        candidates._run_and_freeze_candidate_for_test(
            snapshot=snapshot,
            spec=_spec(candidates),
            attempts_root=tmp_path / "attempts",
            controller_source=controller,
            controller_python=Path(sys.executable),
            runtime_read_paths=(),
            expected_controller=_controller_identity(
                controller, repository_id=snapshot.repo.repository_id,
            ),
            authority_client=authority,
            timeout_seconds=5,
            attempt_id="stdout-overflow",
            sandbox_factory=lambda **kwargs: OverflowLaunch(kwargs, events),
            process_identity_resolver=identity,
            process_group_reaper=candidates.terminate_process_group,
        )
    assert time.monotonic() - started < 3


def test_extinction_proof_failure_retains_base_ref_for_reconciliation(tmp_path):
    candidates = _candidates()
    repo = _repo(tmp_path / "repo")
    snapshot = _snapshot(repo)
    controller = tmp_path / "controller-n-1"
    (controller / "agent").mkdir(parents=True)
    (controller / "agent" / "bestplan_worker.py").write_text("# retained worker\n")
    events: list[object] = []
    authority = _FakeAuthority(events)
    from agent.bestplan_authority_client import WorkerIdentity

    identity = WorkerIdentity(
        4242,
        os.getuid(),
        "proof-start:4242",
        hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest(),
    )

    def failed_reaper(_process, **_kwargs):
        raise candidates.CandidateExecutionError("extinction proof failed")

    with pytest.raises(candidates.CandidateExecutionError):
        candidates._run_and_freeze_candidate_for_test(
            snapshot=snapshot,
            spec=_spec(candidates),
            attempts_root=tmp_path / "attempts",
            controller_source=controller,
            controller_python=Path(sys.executable),
            runtime_read_paths=(),
            expected_controller=_controller_identity(
                controller, repository_id=snapshot.repo.repository_id,
            ),
            authority_client=authority,
            timeout_seconds=2,
            attempt_id="extinction-unproven",
            sandbox_factory=lambda **kwargs: _FakeLaunch(kwargs, events=events),
            process_identity_resolver=lambda _pid, _exe: identity,
            process_group_reaper=failed_reaper,
        )

    base_ref = "refs/hermes-bestplan-bases/bp-plan-1/code/extinction-unproven"
    assert _git(repo, "rev-parse", base_ref) == snapshot.head_oid


def test_run_registers_real_worker_identity_keeps_capability_parent_side_and_revokes_after_reap(
    tmp_path, monkeypatch,
):
    candidates = _candidates()
    snapshot = _snapshot(_repo(tmp_path / "repo"))
    spec = _spec(candidates)
    controller = tmp_path / "controller-n-1"
    worker_script = controller / "agent" / "bestplan_worker.py"
    worker_script.parent.mkdir(parents=True)
    worker_script.write_text("# retained controller worker\n")
    events: list[object] = []
    authority = _FakeAuthority(events)
    launches = []

    def sandbox_factory(**kwargs):
        launch = _FakeLaunch(kwargs, events=events)
        launches.append(launch)
        return launch

    def reaper(process, **_kwargs):
        _observe_broker_close_before_reap(process, events)

    original_freeze = candidates._freeze_sealed_candidate

    def observed_freeze(*args, **kwargs):
        events.append(("freeze", 4242))
        return original_freeze(*args, **kwargs)

    monkeypatch.setattr(candidates, "_freeze_sealed_candidate", observed_freeze)

    from agent.bestplan_authority_client import WorkerIdentity

    identity = WorkerIdentity(
        pid=4242,
        uid=os.getuid(),
        process_start_id="boot:4242:1",
        executable_sha256=hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
    )
    frozen = candidates._run_and_freeze_candidate_for_test(
        snapshot=snapshot,
        spec=spec,
        attempts_root=tmp_path / "attempts",
        controller_source=controller,
        controller_python=Path(sys.executable),
        runtime_read_paths=(),
        expected_controller=_controller_identity(
            controller, repository_id=snapshot.repo.repository_id,
        ),
        authority_client=authority,
        timeout_seconds=5,
        attempt_id="brokered",
        sandbox_factory=sandbox_factory,
        process_identity_resolver=lambda _pid, _exe: identity,
        process_group_reaper=reaper,
    )

    launch = launches[0]
    assert launch.argv[:5] == [
        str(Path(sys.executable).resolve()), "-I", "-S", "-B", "-c",
    ]
    assert launch.argv[5] == candidates.CANDIDATE_BOOTSTRAP
    assert launch.argv[6:8] == [str(controller.resolve()), str(worker_script.resolve())]
    assert launch.kwargs["start_new_session"] is True
    assert launch.kwargs["pass_fds"]
    assert "PYTHONPATH" not in launch.kwargs["env"]
    payload_text = json.dumps(launch.process.payload)
    assert "opaque-capability-must-stay-parent-side" not in payload_text
    assert "api_key" not in payload_text
    assert "provider" not in payload_text
    assert "base_url" not in payload_text
    assert events[0][0] == "register"
    assert events[0][1] == "brokered"
    assert events[0][2] == identity
    assert events[0][3:] == (
        spec.model, spec.request_budget, spec.token_budget, spec.expires_at,
    )
    names = [event[0] for event in events]
    assert names.index("broker_closed") < names.index("reap")
    assert names.index("reap") < names.index("revoke")
    assert names.index("revoke") < names.index("verify_identity")
    assert names.index("verify_identity") < names.index("freeze")
    verify_event = next(event for event in events if event[0] == "verify_identity")
    assert isinstance(verify_event[2], float) and verify_event[2] > time.monotonic()
    assert sum(event[0] == "model_request" for event in events) == 1
    assert launch.closed is True
    assert frozen.commit_oid == _git(
        Path(snapshot.repo.worktree), "rev-parse", frozen.ref_name,
    )


def test_runner_waits_for_sandbox_exec_transition_before_model_admission(tmp_path):
    candidates = _candidates()
    snapshot = _snapshot(_repo(tmp_path / "repo"))
    spec = _spec(candidates)
    controller = tmp_path / "controller-n-1"
    worker_script = controller / "agent" / "bestplan_worker.py"
    worker_script.parent.mkdir(parents=True)
    worker_script.write_text("# retained controller worker\n")
    events: list[object] = []
    authority = _FakeAuthority(events)
    from agent.bestplan_authority_client import WorkerIdentity

    expected_digest = hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest()
    identities = iter((
        WorkerIdentity(4242, os.getuid(), "boot:4242:1", "b" * 64),
        WorkerIdentity(4242, os.getuid(), "boot:4242:1", expected_digest),
    ))
    resolver_calls = []

    def resolver(pid, executable):
        resolver_calls.append((pid, Path(executable).resolve()))
        try:
            return next(identities)
        except StopIteration:
            return WorkerIdentity(4242, os.getuid(), "boot:4242:1", expected_digest)

    frozen = candidates._run_and_freeze_candidate_for_test(
        snapshot=snapshot,
        spec=spec,
        attempts_root=tmp_path / "attempts",
        controller_source=controller,
        controller_python=Path(sys.executable),
        runtime_read_paths=(),
        expected_controller=_controller_identity(
            controller, repository_id=snapshot.repo.repository_id,
        ),
        authority_client=authority,
        timeout_seconds=15,
        attempt_id="exec-transition",
        sandbox_factory=lambda **kwargs: _FakeLaunch(kwargs, events=events),
        process_identity_resolver=resolver,
        process_group_reaper=lambda process, **_kwargs: setattr(process, "returncode", 0),
    )
    assert len(resolver_calls) >= 2
    assert events[0][0] == "register"
    assert events[0][2].executable_sha256 == expected_digest
    assert frozen.commit_oid


def test_timeout_reaps_process_group_then_revokes_and_never_freezes(tmp_path):
    candidates = _candidates()
    snapshot = _snapshot(_repo(tmp_path / "repo"))
    spec = _spec(candidates)
    controller = tmp_path / "controller-n-1"
    worker_script = controller / "agent" / "bestplan_worker.py"
    worker_script.parent.mkdir(parents=True)
    worker_script.write_text("# retained controller worker\n")
    events: list[object] = []
    authority = _FakeAuthority(events)
    launch = None

    def sandbox_factory(**kwargs):
        nonlocal launch
        launch = _FakeLaunch(kwargs, timeout=True, events=events)
        return launch

    from agent.bestplan_authority_client import WorkerIdentity

    identity = WorkerIdentity(
        4242,
        os.getuid(),
        "boot:4242:1",
        hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest(),
    )
    with pytest.raises(candidates.CandidateExecutionError, match="timeout"):
        candidates._run_and_freeze_candidate_for_test(
            snapshot=snapshot,
            spec=spec,
            attempts_root=tmp_path / "attempts",
            controller_source=controller,
            controller_python=Path(sys.executable),
            runtime_read_paths=(),
            expected_controller=_controller_identity(
                controller, repository_id=snapshot.repo.repository_id,
            ),
            authority_client=authority,
            timeout_seconds=15,
            attempt_id="timeout",
            sandbox_factory=sandbox_factory,
            process_identity_resolver=lambda _pid, _exe: identity,
            process_group_reaper=lambda process, **_kwargs: (
                _observe_broker_close_before_reap(process, events),
                setattr(process, "returncode", -9),
            ),
        )

    names = [event[0] for event in events]
    assert names.index("broker_closed") < names.index("reap") < names.index("revoke")
    assert launch is not None and launch.closed is True
    assert not _git(Path(snapshot.repo.worktree), "for-each-ref", "--format=%(refname)", "refs/hermes-bestplan")


def test_runner_rejects_unadmitted_worker_tool_request_before_authority_dispatch(
    tmp_path,
):
    candidates = _candidates()
    snapshot = _snapshot(_repo(tmp_path / "repo"))
    spec = _spec(candidates)
    controller = tmp_path / "controller-n-1"
    worker_script = controller / "agent" / "bestplan_worker.py"
    worker_script.parent.mkdir(parents=True)
    worker_script.write_text("# retained controller worker\n")
    events: list[object] = []
    authority = _FakeAuthority(events)

    class HostileProcess(_FakeProcess):
        def communicate(self, value, timeout=None):
            self.payload = json.loads(value)
            channel = socket.socket(fileno=self.broker_fd)
            _send_frame(channel, {
                "max_output_tokens": 128,
                "request": {
                    "model": self.payload["runtime"]["model"],
                    "messages": [{"role": "user", "content": "run"}],
                    "tools": [{
                        "type": "function",
                        "function": {
                            "name": "terminal", "parameters": {"type": "object"},
                        },
                    }],
                    "max_tokens": 128,
                    "stream": False,
                },
                "request_id": "turn-00000001",
            })
            channel.settimeout(1)
            try:
                _receive_frame(channel)
            except (EOFError, OSError, TimeoutError):
                pass
            channel.close()
            self.returncode = 1
            return "HERMES_BESTPLAN_RESULT={\"status\":\"error\"}", ""

    class HostileLaunch(_FakeLaunch):
        def launch_worker(self):
            self.process = HostileProcess(self.workspace, self.kwargs["pass_fds"][0])
            return self.process

    from agent.bestplan_authority_client import WorkerIdentity

    identity = WorkerIdentity(
        4242,
        os.getuid(),
        "boot:4242:1",
        hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest(),
    )
    with pytest.raises(candidates.CandidateExecutionError, match="broker"):
        candidates._run_and_freeze_candidate_for_test(
            snapshot=snapshot,
            spec=spec,
            attempts_root=tmp_path / "attempts",
            controller_source=controller,
            controller_python=Path(sys.executable),
            runtime_read_paths=(),
            expected_controller=_controller_identity(
                controller, repository_id=snapshot.repo.repository_id,
            ),
            authority_client=authority,
            timeout_seconds=15,
            attempt_id="hostile-tool",
            sandbox_factory=lambda **kwargs: HostileLaunch(kwargs, events=events),
            process_identity_resolver=lambda _pid, _exe: identity,
            process_group_reaper=lambda process, **_kwargs: setattr(
                process, "returncode", 1,
            ),
        )
    assert not any(event[0] == "model_request" for event in events)


def test_worker_output_parser_is_bounded_exactly_once_and_privacy_preserving():
    candidates = _candidates()
    marker = "HERMES_BESTPLAN_RESULT="
    valid = marker + json.dumps({
        "status": "completed", "summary": "ok", "error": None, "api_calls": 1,
    })
    assert candidates.parse_bounded_worker_output(valid, "")["status"] == "completed"
    with pytest.raises(candidates.CandidateExecutionError, match="output limit"):
        candidates.parse_bounded_worker_output(
            "x" * (candidates.MAX_WORKER_STDOUT_BYTES + 1), "",
        )
    with pytest.raises(candidates.CandidateExecutionError, match="result marker"):
        candidates.parse_bounded_worker_output(valid + valid, "")
    sentinel = "credential-endpoint-path-sentinel"
    with pytest.raises(candidates.CandidateExecutionError) as failure:
        candidates.parse_bounded_worker_output(marker + sentinel, sentinel)
    assert sentinel not in str(failure.value)


def _send_frame(channel: socket.socket, value: dict) -> None:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    channel.sendall(struct.pack("!I", len(encoded)) + encoded)


def _receive_exact(channel: socket.socket, size: int) -> bytes:
    output = bytearray()
    while len(output) < size:
        chunk = channel.recv(size - len(output))
        if not chunk:
            raise EOFError
        output.extend(chunk)
    return bytes(output)


def _receive_frame(channel: socket.socket) -> dict:
    size = struct.unpack("!I", _receive_exact(channel, 4))[0]
    return json.loads(_receive_exact(channel, size))


def _write_file_schema() -> dict:
    from agent.bestplan_worker import CANDIDATE_TOOL_SCHEMAS

    return {"type": "function", "function": CANDIDATE_TOOL_SCHEMAS["write_file"]}


def test_worker_broker_shim_round_trips_full_tool_call_without_network_client():
    from agent import bestplan_worker as worker

    parent, child = socket.socketpair()
    observed = {}

    def server():
        request = _receive_frame(parent)
        observed.update(request)
        response = {
            "id": "chatcmpl-tool",
            "object": "chat.completion",
            "created": 1,
            "model": "test/model",
            "choices": [{
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({
                                "path": "allowed/result.txt", "content": "ok\n",
                            }, sort_keys=True, separators=(",", ":")),
                        },
                    }],
                },
            }],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
        }
        _send_frame(parent, {
            "ok": True,
            "request_id": request["request_id"],
            "response_json": json.dumps(response, sort_keys=True, separators=(",", ":")),
        })

    thread = threading.Thread(target=server)
    thread.start()
    try:
        client = worker._BrokerOpenAIClient(worker._BrokerChannel(child))
        response = client.chat.completions.create(
            model="test/model",
            messages=[{"role": "user", "content": "edit"}],
            tools=[_write_file_schema()],
            tool_choice="auto",
            max_tokens=128,
            stream=False,
            timeout=3.0,
        )
    finally:
        thread.join(timeout=5)
        parent.close()
        child.close()

    assert observed["request"]["tools"][0]["function"]["name"] == "write_file"
    assert "timeout" not in observed["request"]
    assert response.choices[0].message.tool_calls[0].function.name == "write_file"
    assert response.choices[0].finish_reason == "tool_calls"


def test_worker_runtime_rejects_provider_endpoints_credentials_and_streaming():
    from agent import bestplan_worker as worker

    valid = {
        "model": "test/model",
        "bestplan_toolsets": ["file"],
        "max_output_tokens": 512,
        "request_overrides": {"temperature": 0},
    }
    assert worker._validate_brokered_runtime(valid)["model"] == "test/model"
    for forbidden in (
        "api_key", "base_url", "provider", "endpoint", "command", "acp_command",
    ):
        with pytest.raises(ValueError, match="brokered runtime"):
            worker._validate_brokered_runtime({**valid, forbidden: "forbidden"})
    with pytest.raises(ValueError, match="stream"):
        worker._BrokerOpenAIClient(SimpleNamespace()).chat.completions.create(
            model="test/model", messages=[], stream=True,
        )
    with pytest.raises(ValueError, match="toolset"):
        worker._validate_brokered_runtime({**valid, "bestplan_toolsets": ["terminal"]})
    with pytest.raises(ValueError, match="routing"):
        worker._validate_brokered_runtime({
            **valid,
            "request_overrides": {
                "extra_body": {"api_key": "credential-sentinel"},
            },
        })


def test_worker_installs_no_secret_source_guard_before_agent_import(monkeypatch):
    from agent import bestplan_worker as worker
    from hermes_cli import env_loader

    calls = []
    monkeypatch.setattr(
        env_loader,
        "load_hermes_dotenv",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    worker._install_bestplan_import_guard()

    assert env_loader.load_hermes_dotenv("ignored") is None
    assert calls == []


def test_worker_broker_rejects_unapproved_tool_schema_before_channel_dispatch():
    from agent import bestplan_worker as worker

    class NeverDispatch:
        def request(self, _value):
            raise AssertionError("unapproved tool reached the broker channel")

    client = worker._BrokerOpenAIClient(
        NeverDispatch(), expected_model="test/model", max_output_tokens=128,
    )
    with pytest.raises(ValueError, match="tool"):
        client.chat.completions.create(
            model="test/model",
            messages=[{"role": "user", "content": "run"}],
            tools=[{
                "type": "function",
                "function": {"name": "terminal", "parameters": {"type": "object"}},
            }],
            max_tokens=128,
            stream=False,
        )
    altered = _write_file_schema()
    altered["function"] = dict(altered["function"])
    altered["function"]["description"] = "altered candidate schema"
    with pytest.raises(ValueError, match="tool"):
        client.chat.completions.create(
            model="test/model",
            messages=[{"role": "user", "content": "edit"}],
            tools=[altered],
            max_tokens=128,
            stream=False,
        )


def test_worker_and_host_use_identical_narrow_candidate_tool_schemas():
    candidates = _candidates()
    from agent import bestplan_worker as worker

    assert candidates._HOST_CANDIDATE_TOOL_SCHEMAS == worker.CANDIDATE_TOOL_SCHEMAS


def _make_all_direct_model_paths_fatal(monkeypatch):
    import openai
    import run_agent
    from agent import auxiliary_client, context_compressor, process_bootstrap

    def fatal(*_args, **_kwargs):
        raise AssertionError("non-broker model path was constructed or called")

    for module, names in (
        (run_agent, ("OpenAI", "AsyncOpenAI", "Anthropic", "AsyncAnthropic")),
        (process_bootstrap, ("OpenAI", "_load_openai_cls")),
        (auxiliary_client, (
            "OpenAI", "AsyncOpenAI", "_load_openai_cls", "call_llm", "async_call_llm",
        )),
        (context_compressor, ("call_llm",)),
        (openai, ("OpenAI", "AsyncOpenAI")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, fatal, raising=False)
    try:
        import anthropic
    except ImportError:
        anthropic = None
    if anthropic is not None:
        for name in ("Anthropic", "AsyncAnthropic"):
            monkeypatch.setattr(anthropic, name, fatal, raising=False)


def test_real_aiagent_normal_turn_uses_broker_when_all_direct_clients_are_fatal(
    tmp_path, monkeypatch,
):
    from agent import bestplan_worker as worker
    from agent.delegation_context import bestplan_child_context
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    _make_all_direct_model_paths_fatal(monkeypatch)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    parent, child = socket.socketpair()
    client = worker._BrokerOpenAIClient(
        worker._BrokerChannel(child),
        expected_model="test/model",
        max_output_tokens=128,
    )
    AgentClass = worker._brokered_agent_class(client)
    requests: list[dict] = []
    server_errors: list[BaseException] = []

    def server():
        try:
            request = _receive_frame(parent)
            requests.append(request)
            response = {
                "id": "chatcmpl-normal-broker",
                "object": "chat.completion",
                "created": 1,
                "model": "test/model",
                "choices": [{
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "normal done"},
                }],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                },
            }
            _send_frame(parent, {
                "ok": True,
                "request_id": request["request_id"],
                "response_json": json.dumps(
                    response, sort_keys=True, separators=(",", ":"),
                ),
            })
        except BaseException as exc:
            server_errors.append(exc)

    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    token = set_hermes_home_override(runtime)
    agent = None
    try:
        agent = AgentClass(
            base_url="http://bestplan-broker.invalid/v1",
            api_key="bestplan-broker-no-provider-credential",
            provider="openai",
            api_mode="chat_completions",
            model="test/model",
            max_iterations=2,
            max_tokens=128,
            enabled_toolsets=[],
            quiet_mode=True,
            save_trajectories=False,
            platform="bestplan-worker",
            skip_context_files=True,
            skip_memory=True,
            checkpoints_enabled=False,
        )
        worker._disable_auxiliary_model_paths(agent)
        agent.terminal_cwd = str(tmp_path)
        with bestplan_child_context("normal-broker-only"):
            result = agent.run_conversation(
                user_message="Return the final result.",
                system_message="Use only the inherited model channel.",
                conversation_history=[],
                task_id="normal-broker-only",
            )
    finally:
        if agent is not None:
            agent.close()
        client.close()
        parent.close()
        reset_hermes_home_override(token)
    thread.join(timeout=5)

    assert not server_errors
    assert result["final_response"] == "normal done"
    assert len(requests) == 1
    assert "timeout" not in requests[0]["request"]


def test_real_aiagent_tool_turn_and_stop_use_only_inherited_broker(tmp_path):
    candidates = _candidates()
    from agent import bestplan_sandbox as sandbox
    from agent import bestplan_worker as worker

    workspace = tmp_path / "attempt" / "source"
    runtime = tmp_path / "attempt" / "runtime"
    scratch = tmp_path / "attempt" / "scratch"
    (workspace / "allowed").mkdir(parents=True)
    runtime.mkdir(parents=True)
    scratch.mkdir(parents=True)
    parent, child = socket.socketpair()
    environment = _candidate_environment(
        workspace, runtime, scratch, child.fileno(),
    )
    controller = Path(__file__).resolve().parents[2]
    dependencies = sandbox.pinned_candidate_runtime_paths(Path(sys.executable))
    command = [
        str(Path(sys.executable).resolve()),
        "-I", "-S", "-B", "-c", sandbox.CANDIDATE_BOOTSTRAP,
        str(controller),
        str((controller / "agent" / "bestplan_worker.py").resolve()),
        *(str(path) for path in dependencies),
    ]
    process = subprocess.Popen(
        command,
        cwd=workspace,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        pass_fds=(child.fileno(),),
        start_new_session=True,
    )
    child.close()
    requests: list[dict] = []
    server_errors: list[BaseException] = []

    def server():
        try:
            for index in range(2):
                request = _receive_frame(parent)
                requests.append(request)
                body = request["request"]
                assert "timeout" not in body
                assert len(body["tools"]) == 4
                assert {
                    schema["function"]["name"]: schema["function"]
                    for schema in body["tools"]
                } == worker.CANDIDATE_TOOL_SCHEMAS
                if index == 0:
                    message = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "call-write-1",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps({
                                    "path": "allowed/result.txt",
                                    "content": "written through actual AIAgent\n",
                                }, sort_keys=True, separators=(",", ":")),
                            },
                        }],
                    }
                    finish_reason = "tool_calls"
                else:
                    message = {"role": "assistant", "content": "done"}
                    finish_reason = "stop"
                response = {
                    "id": f"chatcmpl-real-{index}",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "test/model",
                    "choices": [{
                        "index": 0,
                        "finish_reason": finish_reason,
                        "message": message,
                    }],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 2,
                        "total_tokens": 5,
                    },
                }
                _send_frame(parent, {
                    "ok": True,
                    "request_id": request["request_id"],
                    "response_json": json.dumps(
                        response, sort_keys=True, separators=(",", ":"),
                    ),
                })
        except BaseException as exc:
            server_errors.append(exc)

    server_thread = threading.Thread(target=server, daemon=True)
    server_thread.start()
    payload = {
        "allowed_paths": ["allowed/"],
        "goal": "Create the declared result file",
        "max_iterations": 4,
        "read_only": False,
        "runtime": {
            "bestplan_toolsets": ["file"],
            "max_output_tokens": 256,
            "model": "test/model",
            "request_overrides": {},
        },
        "runtime_home": str(runtime.resolve()),
        "system_prompt": "Use the approved file operation and then finish.",
        "task_id": "real-aiagent-turn",
        "workspace": str(workspace.resolve()),
    }
    capture = candidates._BoundedProcessCapture(
        process, json.dumps(payload, sort_keys=True, separators=(",", ":")),
    )
    capture.start()
    try:
        ready = capture.result_ready.wait(20)
        if not ready:
            with capture.lock:
                partial_stdout = bytes(capture.outputs["stdout"]).decode("utf-8", "replace")
                partial_stderr = bytes(capture.outputs["stderr"]).decode("utf-8", "replace")
            pytest.fail(
                "actual AIAgent did not emit a result; "
                f"server_errors={server_errors!r} stdout={partial_stdout!r} "
                f"stderr={partial_stderr!r}"
            )
        parent.shutdown(socket.SHUT_RDWR)
        parent.close()
        assert process.wait(timeout=10) == 0
        stdout, stderr = capture.finish()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        try:
            parent.close()
        except OSError:
            pass
    server_thread.join(timeout=5)

    assert not server_errors
    assert len(requests) == 2
    assert stderr == ""
    assert candidates.parse_bounded_worker_output(stdout, stderr)["summary"] == "done"
    assert (workspace / "allowed" / "result.txt").read_text() == (
        "written through actual AIAgent\n"
    )


def test_fresh_agent_import_guard_blocks_project_profile_and_managed_secret_loaders(tmp_path):
    controller = Path(__file__).resolve().parents[2]
    project = tmp_path / "project"
    runtime = tmp_path / "runtime"
    project.mkdir()
    runtime.mkdir()
    (project / ".env").write_text("PROJECT_SECRET_SENTINEL=forbidden\n")
    (runtime / ".env").write_text("RUNTIME_SECRET_SENTINEL=forbidden\n")
    script = r'''
import json, os
from agent import bestplan_worker as worker
from hermes_cli import env_loader

def fatal(*_args, **_kwargs):
    raise AssertionError("secret loader executed")

for name in (
    "_load_dotenv_with_fallback",
    "_apply_external_secret_sources",
    "_apply_managed_env",
    "_sanitize_loaded_credentials",
    "_sanitize_env_file_if_needed",
):
    setattr(env_loader, name, fatal)
worker._install_bestplan_import_guard()
before = dict(os.environ)
import run_agent
assert dict(os.environ) == before
assert "PROJECT_SECRET_SENTINEL" not in os.environ
assert "RUNTIME_SECRET_SENTINEL" not in os.environ
print(json.dumps({"guarded": True}, sort_keys=True))
'''
    environment = {
        "HOME": str(runtime),
        "HERMES_HOME": str(runtime),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(controller),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout) == {"guarded": True}


def test_real_aiagent_iteration_summary_and_retry_use_primary_broker_only(
    tmp_path, monkeypatch,
):
    from agent import bestplan_worker as worker
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    parent, child = socket.socketpair()
    client = worker._BrokerOpenAIClient(
        worker._BrokerChannel(child),
        expected_model="test/model",
        max_output_tokens=128,
    )
    AgentClass = worker._brokered_agent_class(client)
    _make_all_direct_model_paths_fatal(monkeypatch)
    reasons: list[str] = []

    def broker_primary(self, *, reason):
        reasons.append(reason)
        return client

    monkeypatch.setattr(AgentClass, "_ensure_primary_openai_client", broker_primary)
    requests: list[dict] = []

    def server():
        for index, content in enumerate(("", "summary done")):
            request = _receive_frame(parent)
            requests.append(request)
            response = {
                "id": f"chatcmpl-summary-{index}",
                "object": "chat.completion",
                "created": 1,
                "model": "test/model",
                "choices": [{
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                },
            }
            _send_frame(parent, {
                "ok": True,
                "request_id": request["request_id"],
                "response_json": json.dumps(
                    response, sort_keys=True, separators=(",", ":"),
                ),
            })

    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    token = set_hermes_home_override(runtime)
    agent = None
    try:
        agent = AgentClass(
            base_url="http://bestplan-broker.invalid/v1",
            api_key="bestplan-broker-no-provider-credential",
            provider="openai",
            api_mode="chat_completions",
            model="test/model",
            max_iterations=1,
            max_tokens=128,
            enabled_toolsets=["read_only_files"],
            quiet_mode=True,
            save_trajectories=False,
            platform="bestplan-worker",
            skip_context_files=True,
            skip_memory=True,
            checkpoints_enabled=False,
        )
        worker._disable_auxiliary_model_paths(agent)
        result = agent._handle_max_iterations(
            [{"role": "user", "content": "summarize current work"}], 1,
        )
    finally:
        if agent is not None:
            agent.close()
        client.close()
        parent.close()
        reset_hermes_home_override(token)
    thread.join(timeout=5)

    assert result == "summary done"
    assert reasons == [
        "iteration_limit_summary",
        "iteration_limit_summary_retry",
    ]
    assert len(requests) == 2
    assert all(request["request"].get("tools", []) == [] for request in requests)


@pytest.mark.parametrize("marker", ("", "0", "2", "true", "01"))
def test_any_present_noncanonical_candidate_child_marker_fails_closed(
    marker, monkeypatch, capsys,
):
    from agent import bestplan_worker as worker

    monkeypatch.setenv("HERMES_BESTPLAN_CHILD", marker)
    monkeypatch.delenv("HERMES_BESTPLAN_BROKER_FD", raising=False)
    monkeypatch.setattr(
        worker,
        "_legacy_main",
        lambda: (_ for _ in ()).throw(
            AssertionError("candidate child reached credential-capable legacy path")
        ),
    )

    assert worker._main() == 1
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out.startswith("HERMES_BESTPLAN_RESULT=")
    assert "candidate_broker_unavailable" in output.out


@pytest.mark.parametrize("descriptor", (None, "", "0", "-1", "03", "not-a-fd"))
def test_candidate_child_marker_requires_canonical_connected_broker_fd(
    descriptor, monkeypatch, capsys,
):
    from agent import bestplan_worker as worker

    monkeypatch.setenv("HERMES_BESTPLAN_CHILD", "1")
    if descriptor is None:
        monkeypatch.delenv("HERMES_BESTPLAN_BROKER_FD", raising=False)
    else:
        monkeypatch.setenv("HERMES_BESTPLAN_BROKER_FD", descriptor)
    monkeypatch.setattr(
        worker,
        "_brokered_main",
        lambda: (_ for _ in ()).throw(
            AssertionError("invalid broker descriptor reached brokered worker")
        ),
    )
    monkeypatch.setattr(
        worker,
        "_legacy_main",
        lambda: (_ for _ in ()).throw(
            AssertionError("candidate child reached credential-capable legacy path")
        ),
    )

    assert worker._main() == 1
    output = capsys.readouterr()
    assert output.err == ""
    assert "candidate_broker_unavailable" in output.out


def test_candidate_child_marker_rejects_a_non_socket_descriptor(
    monkeypatch, capsys,
):
    from agent import bestplan_worker as worker

    read_fd, write_fd = os.pipe()
    try:
        monkeypatch.setenv("HERMES_BESTPLAN_CHILD", "1")
        monkeypatch.setenv("HERMES_BESTPLAN_BROKER_FD", str(read_fd))
        monkeypatch.setattr(
            worker,
            "_brokered_main",
            lambda: (_ for _ in ()).throw(
                AssertionError("non-socket descriptor reached brokered worker")
            ),
        )

        assert worker._main() == 1
    finally:
        os.close(read_fd)
        os.close(write_fd)
    output = capsys.readouterr()
    assert output.err == ""
    assert "candidate_broker_unavailable" in output.out


def test_candidate_child_marker_accepts_only_a_connected_unix_stream(
    monkeypatch,
):
    from agent import bestplan_worker as worker

    parent, child = socket.socketpair()
    try:
        monkeypatch.setenv("HERMES_BESTPLAN_CHILD", "1")
        monkeypatch.setenv("HERMES_BESTPLAN_BROKER_FD", str(child.fileno()))
        monkeypatch.setattr(worker, "_brokered_main", lambda: 37)
        assert worker._main() == 37
    finally:
        parent.close()
        child.close()


def test_broker_fd_without_candidate_marker_does_not_enter_either_worker_mode(
    monkeypatch, capsys,
):
    from agent import bestplan_worker as worker

    parent, child = socket.socketpair()
    try:
        monkeypatch.delenv("HERMES_BESTPLAN_CHILD", raising=False)
        monkeypatch.setenv("HERMES_BESTPLAN_BROKER_FD", str(child.fileno()))
        monkeypatch.setattr(
            worker,
            "_brokered_main",
            lambda: (_ for _ in ()).throw(
                AssertionError("markerless descriptor reached brokered worker")
            ),
        )
        monkeypatch.setattr(
            worker,
            "_legacy_main",
            lambda: (_ for _ in ()).throw(
                AssertionError("markerless descriptor reached legacy worker")
            ),
        )

        assert worker._main() == 1
    finally:
        parent.close()
        child.close()
    output = capsys.readouterr()
    assert output.err == ""
    assert "candidate_broker_unavailable" in output.out


@pytest.mark.parametrize("bad_response", [
    {
        "id": "chatcmpl-model",
        "object": "chat.completion",
        "created": 1,
        "model": "substituted/model",
        "choices": [{
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "done"},
        }],
    },
    {
        "id": "chatcmpl-tool",
        "object": "chat.completion",
        "created": 1,
        "model": "test/model",
        "choices": [{
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call-terminal",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": "{}"},
                }],
            },
        }],
    },
])
def test_worker_broker_rejects_response_model_or_unadvertised_tool(bad_response):
    from agent import bestplan_worker as worker

    parent, child = socket.socketpair()

    def server():
        request = _receive_frame(parent)
        _send_frame(parent, {
            "ok": True,
            "request_id": request["request_id"],
            "response_json": json.dumps(
                bad_response, sort_keys=True, separators=(",", ":"),
            ),
        })

    thread = threading.Thread(target=server)
    thread.start()
    try:
        client = worker._BrokerOpenAIClient(
            worker._BrokerChannel(child),
            expected_model="test/model",
            max_output_tokens=128,
        )
        with pytest.raises(ValueError, match="model|tool"):
            client.chat.completions.create(
                model="test/model",
                messages=[{"role": "user", "content": "edit"}],
                tools=[_write_file_schema()],
                max_tokens=128,
                stream=False,
            )
    finally:
        thread.join(timeout=5)
        parent.close()
        child.close()


def test_worker_file_tools_are_in_process_lease_bound_and_do_not_follow_escape_symlinks(
    tmp_path,
):
    from agent import bestplan_worker as worker

    workspace = tmp_path / "source"
    allowed = workspace / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir(parents=True)
    outside.mkdir()
    (outside / "secret.txt").write_text("outside\n")
    (allowed / "escape").symlink_to(outside, target_is_directory=True)
    tools = worker._CandidateFileTools(
        workspace=workspace,
        allowed_paths=("allowed/",),
        read_only=False,
    )

    written = json.loads(tools.write({"path": "allowed/result.txt", "content": "ok\n"}))
    assert written["verified"] is True
    assert (allowed / "result.txt").read_text() == "ok\n"
    assert "ok" in tools.read({"path": "allowed/result.txt"})
    assert "result.txt" in tools.search({
        "pattern": "result.txt", "target": "files", "path": "allowed",
    })
    with pytest.raises(ValueError, match="lease"):
        tools.write({"path": "outside.txt", "content": "bad"})
    with pytest.raises(ValueError, match="symlink"):
        tools.write({"path": "allowed/escape/secret.txt", "content": "bad"})
    assert (outside / "secret.txt").read_text() == "outside\n"


def test_worker_file_tools_bound_aggregate_operations_per_attempt(tmp_path, monkeypatch):
    from agent import bestplan_worker as worker

    workspace = tmp_path / "source"
    allowed = workspace / "allowed"
    allowed.mkdir(parents=True)
    (allowed / "base.txt").write_text("base\n")
    monkeypatch.setattr(
        worker, "_MAX_CANDIDATE_TOOL_OPERATIONS", 1, raising=False,
    )
    tools = worker._CandidateFileTools(
        workspace=workspace, allowed_paths=("allowed/",), read_only=False,
    )

    assert "base" in tools.read({"path": "allowed/base.txt"})
    with pytest.raises(ValueError, match="operation budget"):
        tools.read({"path": "allowed/base.txt"})


def test_worker_file_tools_bound_aggregate_read_and_write_bytes(tmp_path, monkeypatch):
    from agent import bestplan_worker as worker

    workspace = tmp_path / "source"
    allowed = workspace / "allowed"
    allowed.mkdir(parents=True)
    (allowed / "base.txt").write_text("abcd")
    monkeypatch.setattr(worker, "_MAX_CANDIDATE_READ_BYTES", 3, raising=False)
    monkeypatch.setattr(worker, "_MAX_CANDIDATE_WRITE_BYTES", 3, raising=False)

    read_tools = worker._CandidateFileTools(
        workspace=workspace, allowed_paths=("allowed/",), read_only=False,
    )
    with pytest.raises(ValueError, match="read byte budget"):
        read_tools.read({"path": "allowed/base.txt"})

    write_tools = worker._CandidateFileTools(
        workspace=workspace, allowed_paths=("allowed/",), read_only=False,
    )
    with pytest.raises(ValueError, match="write byte budget"):
        write_tools.write({"path": "allowed/result.txt", "content": "abcd"})
    assert not (allowed / "result.txt").exists()


def test_worker_file_tools_bound_aggregate_search_bytes_across_calls(tmp_path, monkeypatch):
    from agent import bestplan_worker as worker

    workspace = tmp_path / "source"
    allowed = workspace / "allowed"
    allowed.mkdir(parents=True)
    (allowed / "base.txt").write_text("abc\n")
    monkeypatch.setattr(worker, "_MAX_CANDIDATE_SEARCH_BYTES", 7, raising=False)
    tools = worker._CandidateFileTools(
        workspace=workspace, allowed_paths=("allowed/",), read_only=False,
    )

    assert "base.txt" in tools.search({
        "pattern": "abc", "target": "content", "path": "allowed/base.txt",
    })
    with pytest.raises(ValueError, match="search byte budget"):
        tools.search({
            "pattern": "abc", "target": "content", "path": "allowed/base.txt",
        })


def test_worker_patch_checks_input_and_projected_output_bounds_before_read_or_replace(
    tmp_path, monkeypatch,
):
    from agent import bestplan_worker as worker

    workspace = tmp_path / "source"
    allowed = workspace / "allowed"
    allowed.mkdir(parents=True)
    oversized = allowed / "oversized.txt"
    with oversized.open("wb") as handle:
        handle.truncate(worker._MAX_FILE_BYTES + 1)
    tools = worker._CandidateFileTools(
        workspace=workspace, allowed_paths=("allowed/",), read_only=False,
    )
    original_read_text = Path.read_text

    def guarded_read_text(path, *args, **kwargs):
        if path == oversized:
            raise AssertionError("oversized input was read before its stat bound")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    with pytest.raises(ValueError, match="bounded"):
        tools.patch({
            "path": "allowed/oversized.txt",
            "old_string": "a",
            "new_string": "b",
            "replace_all": True,
        })

    small = allowed / "small.txt"
    small.write_text("aaaa", encoding="utf-8")
    monkeypatch.setattr(worker, "_MAX_FILE_BYTES", 16)
    monkeypatch.setattr(
        tools,
        "_atomic_write",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("oversized replacement was constructed")
        ),
    )
    with pytest.raises(ValueError, match="bounded"):
        tools.patch({
            "path": "allowed/small.txt",
            "old_string": "a",
            "new_string": "0123456789",
            "replace_all": True,
        })


def test_worker_search_stops_during_streaming_traversal_at_entry_bound(
    tmp_path, monkeypatch,
):
    from agent import bestplan_worker as worker

    workspace = tmp_path / "source"
    allowed = workspace / "allowed"
    allowed.mkdir(parents=True)
    first = allowed / "first.txt"
    second = allowed / "second.txt"
    first.write_text("first\n")
    second.write_text("second\n")
    tools = worker._CandidateFileTools(
        workspace=workspace, allowed_paths=("allowed/",), read_only=False,
    )
    monkeypatch.setattr(worker, "_MAX_SEARCH_ENTRIES", 1)
    original_rglob = Path.rglob

    def bounded_probe(path, pattern):
        if path == allowed:
            yield first
            yield second
            raise AssertionError("search traversal buffered past the entry bound")
        yield from original_rglob(path, pattern)

    monkeypatch.setattr(Path, "rglob", bounded_probe)
    with pytest.raises(ValueError, match="entry limit"):
        tools.search({"pattern": "*.txt", "target": "files", "path": "allowed"})


def test_worker_content_search_treats_pattern_as_literal_text(tmp_path):
    from agent import bestplan_worker as worker

    workspace = tmp_path / "source"
    allowed = workspace / "allowed"
    allowed.mkdir(parents=True)
    (allowed / "literal.txt").write_text("literal [ marker\n")
    tools = worker._CandidateFileTools(
        workspace=workspace, allowed_paths=("allowed/",), read_only=False,
    )
    assert "literal.txt:1:" in tools.search({
        "pattern": "[", "target": "content", "path": "allowed",
    })


def test_brokered_agent_configuration_disables_every_auxiliary_model_path():
    from agent import bestplan_worker as worker

    agent = SimpleNamespace(
        compression_enabled=True,
        context_compressor=SimpleNamespace(_micro_compact_enabled=True),
    )
    worker._disable_auxiliary_model_paths(agent)
    assert agent.compression_enabled is False
    assert agent.context_compressor._micro_compact_enabled is False


def test_real_process_group_reaper_terminates_descendants(tmp_path):
    candidates = _candidates()
    marker = tmp_path / "descendant-survived"
    child_code = (
        "import pathlib,time; time.sleep(1); "
        f"pathlib.Path({str(marker)!r}).write_text('bad')"
    )
    parent_code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        "time.sleep(30)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", parent_code], start_new_session=True,
    )
    candidates.terminate_process_group(process, grace_seconds=0.2)
    assert process.poll() is not None
    time.sleep(1.2)
    assert not marker.exists()
    with pytest.raises(ProcessLookupError):
        os.killpg(process.pid, 0)


def test_process_group_reaper_kills_descendant_after_leader_already_exited(tmp_path):
    candidates = _candidates()
    marker = tmp_path / "escaped-descendant"
    child_code = (
        "import pathlib,signal,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "time.sleep(1);"
        f"pathlib.Path({str(marker)!r}).write_text('bad')"
    )
    parent_code = (
        "import subprocess,sys;"
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
        "print(p.pid,flush=True)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", parent_code],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    child_pid = int(process.stdout.readline().strip())
    process.wait(timeout=5)
    try:
        candidates.terminate_process_group(process, grace_seconds=0.1)
        time.sleep(1.1)
        assert not marker.exists()
    finally:
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_process_group_reaper_fails_closed_when_process_observation_fails(monkeypatch):
    candidates = _candidates()

    class FailedObserver:
        returncode = 1

        class Output:
            @staticmethod
            def read1(_size):
                return b""

        stdout = Output()

        @staticmethod
        def poll():
            return 1

        @staticmethod
        def wait(**_kwargs):
            return 1

    class ExitedLeader:
        pid = 987654
        returncode = 0

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait(**_kwargs):
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: FailedObserver())
    monkeypatch.setattr(
        os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(ProcessLookupError()),
    )

    with pytest.raises(candidates.CandidateExecutionError, match="observation"):
        candidates.terminate_process_group(ExitedLeader(), grace_seconds=0)


@pytest.mark.parametrize("mode", ("hang", "oversized"))
def test_process_group_observation_is_deadline_and_output_bounded(monkeypatch, mode):
    candidates = _candidates()
    released = threading.Event()

    class FakeObserver:
        pid = 777777
        returncode = None

        @property
        def stdout(self):
            return self

        def read1(self, _size):
            if mode == "oversized":
                if self.returncode is None:
                    self.returncode = 0
                    return b"x" * (4 * 1024 * 1024 + 1)
                return b""
            released.wait(2)
            return b""

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -signal.SIGKILL
            released.set()

        def wait(self, **_kwargs):
            released.set()
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: FakeObserver())
    started = time.monotonic()

    with pytest.raises(candidates.CandidateExecutionError, match="observation"):
        candidates._observe_process_group_members(
            987654, deadline=time.monotonic() + 0.05,
        )

    assert time.monotonic() - started < 1


@pytest.mark.skipif(sys.platform != "darwin", reason="exercises Darwin proc_pidinfo")
def test_default_process_identity_uses_kernel_start_time_without_ps(monkeypatch):
    candidates = _candidates()
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("locale/second-resolution ps identity is forbidden")
            ),
        )

        identity = candidates._default_process_identity(process.pid, Path(sys.executable))

        assert identity.pid == process.pid
        assert identity.uid == os.getuid()
        assert identity.process_start_id.startswith("darwin-start:")
        assert len(identity.process_start_id.split(":")) == 4
        assert identity.executable_sha256 == hashlib.sha256(
            Path(sys.executable).resolve().read_bytes(),
        ).hexdigest()
    finally:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
