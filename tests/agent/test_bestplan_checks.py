from __future__ import annotations

import base64
import copy
import errno
import hashlib
import inspect
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest


def _checks():
    from agent import bestplan_checks

    return bestplan_checks


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_checker_launch_uses_exact_absolute_argv_cwd_env_and_shell_false(
    tmp_path, monkeypatch,
):
    checks = _checks()
    executable = tmp_path / "checker"
    executable.write_bytes(b"checker\n")
    executable.chmod(0o755)
    cwd = tmp_path / "integration"
    cwd.mkdir()
    profile = tmp_path / "policy.sb"
    profile.write_text("(version 1)(allow default)\n", encoding="utf-8")
    seen: dict[str, object] = {}

    class _Process:
        pid = 123
        stdin = None
        stdout = None
        stderr = None

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen.update(kwargs)
        return _Process()

    monkeypatch.setattr(checks.subprocess, "Popen", fake_popen)
    process = checks._launch_check_process(
        executable=executable,
        argv=("--flag", "literal value"),
        cwd=cwd,
        environment={"LANG": "C", "BOUND": "exact"},
        profile_path=profile,
    )

    assert isinstance(process, _Process)
    assert seen["argv"] == [
        "/usr/bin/sandbox-exec", "-f", str(profile),
        str(executable), "--flag", "literal value",
    ]
    assert seen["cwd"] == str(cwd)
    assert seen["env"] == {"LANG": "C", "BOUND": "exact"}
    assert seen["shell"] is False
    assert seen["start_new_session"] is True
    assert seen["close_fds"] is True


def test_check_profile_is_default_deny_and_grants_only_frozen_roots(tmp_path):
    checks = _checks()
    integration = tmp_path / "integration"
    runtime = tmp_path / "runtime"
    scratch = tmp_path / "scratch"
    cache = integration / ".cache" / "pytest"
    executable = tmp_path / "bin" / "checker"
    dependency = tmp_path / "runtime-dependency"
    for directory in (integration, runtime, scratch, cache, executable.parent, dependency):
        directory.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"checker\n")
    executable.chmod(0o755)

    profile = checks._check_profile_text(
        integration_root=integration,
        runtime_root=runtime,
        scratch_root=scratch,
        cache_roots=(cache,),
        executable=executable,
        runtime_read_paths=(dependency,),
        network_allowlist=(),
    )

    assert "(deny default)" in profile
    assert "(deny network*)" in profile
    assert "(deny mach-lookup)" in profile
    assert "(deny signal)" in profile
    assert "(deny process-info*)" in profile
    assert "(deny process-fork)" in profile
    assert "(allow process-fork)" not in profile
    assert f'(allow process-exec (literal "{executable}"))' in profile
    assert f'(allow file-read* (subpath "{integration}"))' in profile
    assert f'(allow file-write* (subpath "{cache}"))' in profile
    assert f'(allow file-write* (subpath "{integration}"))' not in profile
    assert f'(allow file-read* (subpath "{tmp_path.parent}"))' not in profile


@pytest.mark.parametrize(
    "endpoint",
    (
        "localhost:443",
        "https://127.0.0.1:443",
        "127.0.0.1",
        "127.0.0.1:0",
        "127.0.0.1:65536",
        "/private/tmp/service.sock",
        "user@127.0.0.1:443",
    ),
)
def test_network_allowlist_rejects_ambiguous_or_nonliteral_endpoints(endpoint):
    checks = _checks()

    with pytest.raises(checks.CheckValidationError):
        checks.parse_network_allowlist((endpoint,))


def test_network_allowlist_renders_only_exact_numeric_tcp_endpoints(tmp_path):
    checks = _checks()
    roots = [tmp_path / name for name in ("integration", "runtime", "scratch")]
    for root in roots:
        root.mkdir()
    executable = tmp_path / "checker"
    executable.write_bytes(b"checker")
    executable.chmod(0o755)

    endpoints = checks.parse_network_allowlist(("127.0.0.1:4318", "[::1]:4319"))
    profile = checks._check_profile_text(
        integration_root=roots[0],
        runtime_root=roots[1],
        scratch_root=roots[2],
        cache_roots=(),
        executable=executable,
        runtime_read_paths=(),
        network_allowlist=endpoints,
    )

    assert '(allow network-outbound (remote tcp "127.0.0.1:4318"))' in profile
    assert '(allow network-outbound (remote tcp "[::1]:4319"))' in profile
    assert "network-inbound" not in profile


def test_pinned_executable_and_config_are_revalidated_before_launch(tmp_path):
    checks = _checks()
    executable = tmp_path / "checker"
    config = tmp_path / "integration" / "checker.toml"
    config.parent.mkdir()
    executable.write_bytes(b"v1")
    executable.chmod(0o755)
    config.write_bytes(b"mode='strict'\n")

    assert checks.pinned_path_sha256(executable) == _sha256(executable)
    assert checks.pinned_path_sha256(config) == _sha256(config)
    checks._require_pinned_regular_file(executable, _sha256(executable), "checker")
    checks._require_pinned_regular_file(config, _sha256(config), "config")

    executable.write_bytes(b"v2")
    config.write_bytes(b"mode='relaxed'\n")
    with pytest.raises(checks.CheckProofStale):
        checks._require_pinned_regular_file(executable, hashlib.sha256(b"v1").hexdigest(), "checker")
    with pytest.raises(checks.CheckProofStale):
        checks._require_pinned_regular_file(config, hashlib.sha256(b"mode='strict'\n").hexdigest(), "config")


def test_receipt_output_hashes_are_bound_to_exact_integration_oid():
    checks = _checks()
    command_digest = "a" * 64
    first = checks._build_check_receipt(
        integration_oid="1" * 40,
        command_id="focused",
        command_digest=command_digest,
        policy_digest="b" * 64,
        exit_code=0,
        stdout=b"green\n",
        stderr=b"",
        pre_tree_digest="c" * 64,
        post_tree_digest="c" * 64,
    )
    second = checks._build_check_receipt(
        integration_oid="2" * 40,
        command_id="focused",
        command_digest=command_digest,
        policy_digest="b" * 64,
        exit_code=0,
        stdout=b"green\n",
        stderr=b"",
        pre_tree_digest="c" * 64,
        post_tree_digest="c" * 64,
    )

    assert first.stdout_sha256 == hashlib.sha256(b"green\n").hexdigest()
    assert first.stderr_sha256 == hashlib.sha256(b"").hexdigest()
    assert first.output_framed_sha256 != second.output_framed_sha256
    assert first.receipt_digest != second.receipt_digest


def test_raw_overlay_rejects_tracked_mutation_even_beneath_cache_parent(tmp_path):
    checks = _checks()
    root = tmp_path / "integration"
    cache = root / ".cache" / "pytest"
    tracked = root / ".cache" / "tracked.cfg"
    cache.mkdir(parents=True)
    tracked.write_bytes(b"strict\n")
    before = checks._capture_check_tree(root, deadline=time.monotonic() + 5)

    tracked.write_bytes(b"relaxed\n")
    after = checks._capture_check_tree(root, deadline=time.monotonic() + 5)
    with pytest.raises(checks.CheckMutationError, match="tracked"):
        checks._validate_overlay_mutations(
            baseline=before,
            current=after,
            tracked_paths=(b".cache/tracked.cfg",),
            cache_paths=(b".cache/pytest",),
        )


def test_raw_overlay_ignores_gitignore_and_allows_only_frozen_cache(tmp_path):
    checks = _checks()
    root = tmp_path / "integration"
    cache = root / ".cache" / "pytest"
    cache.mkdir(parents=True)
    (root / ".gitignore").write_text("*.leak\n", encoding="utf-8")
    before = checks._capture_check_tree(root, deadline=time.monotonic() + 5)

    (cache / "result.bin").write_bytes(b"allowed")
    allowed = checks._capture_check_tree(root, deadline=time.monotonic() + 5)
    checks._validate_overlay_mutations(
        baseline=before,
        current=allowed,
        tracked_paths=(b".gitignore",),
        cache_paths=(b".cache/pytest",),
    )

    (root / "secret.leak").write_bytes(b"ignored by candidate, not by host")
    leaked = checks._capture_check_tree(root, deadline=time.monotonic() + 5)
    with pytest.raises(checks.CheckMutationError, match="outside"):
        checks._validate_overlay_mutations(
            baseline=before,
            current=leaked,
            tracked_paths=(b".gitignore",),
            cache_paths=(b".cache/pytest",),
        )


@pytest.mark.parametrize("terminal", ("success", "failure", "cancel", "timeout"))
def test_process_tree_is_reaped_on_every_terminal_path(terminal):
    checks = _checks()
    cancel = threading.Event()
    if terminal == "success":
        child = "import time; time.sleep(30)"
        body = (
            "import subprocess,sys;"
            f"subprocess.Popen([sys.executable,'-c',{child!r}]);"
            "print('ok')"
        )
        deadline = time.monotonic() + 3
    elif terminal == "failure":
        body = "import sys; print('bad'); sys.exit(7)"
        deadline = time.monotonic() + 3
    else:
        body = "import time; time.sleep(30)"
        deadline = time.monotonic() + (3 if terminal == "cancel" else 0.2)
    process = subprocess.Popen(
        [sys.executable, "-c", body],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    if terminal == "cancel":
        threading.Timer(0.1, cancel.set).start()

    if terminal in {"cancel", "timeout"}:
        with pytest.raises(checks.CheckExecutionError):
            checks._supervise_check_process(
                process,
                deadline=deadline,
                cancel_event=cancel,
                max_output_bytes=1024 * 1024,
                reap_grace_seconds=0.2,
            )
    else:
        captured = checks._supervise_check_process(
            process,
            deadline=deadline,
            cancel_event=cancel,
            max_output_bytes=1024 * 1024,
            reap_grace_seconds=0.2,
        )
        assert captured.returncode == (0 if terminal == "success" else 7)
    with pytest.raises(ProcessLookupError):
        os.killpg(process.pid, 0)


def test_sigterm_ignoring_timeout_uses_cleanup_reserve_and_reaps():
    checks = _checks()
    body = (
        "import signal,time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "time.sleep(30)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", body],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    reap_grace = 0.2
    global_deadline = time.monotonic() + 0.8
    run_deadline = global_deadline - (2 * reap_grace)

    try:
        with pytest.raises(
            checks.CheckExecutionError,
            match=r"^check timeout$",
        ):
            checks._supervise_check_process(
                process,
                deadline=run_deadline,
                cancel_event=None,
                max_output_bytes=1024 * 1024,
                reap_grace_seconds=reap_grace,
                cleanup_deadline=global_deadline,
            )

        assert process.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file(),
    reason="requires the real macOS sandbox-exec backend",
)
def test_real_check_profile_denies_private_read_tracked_write_parent_signal_and_fork(
    tmp_path,
):
    from agent.bestplan_sandbox import pinned_candidate_runtime_paths

    checks = _checks()
    integration = tmp_path / "integration"
    runtime = tmp_path / "runtime"
    scratch = tmp_path / "scratch"
    cache = integration / ".cache" / "check"
    private = tmp_path / "private.txt"
    executable = Path(sys.executable).resolve(strict=True)
    for path in (integration, runtime, scratch, cache):
        path.mkdir(parents=True, exist_ok=True)
    tracked = integration / "tracked.txt"
    tracked.write_text("tracked\n", encoding="utf-8")
    private.write_text("private sentinel\n", encoding="utf-8")
    profile = checks._check_profile_text(
        integration_root=integration,
        runtime_root=runtime,
        scratch_root=scratch,
        cache_roots=(cache,),
        executable=executable,
        runtime_read_paths=pinned_candidate_runtime_paths(sys.executable),
        network_allowlist=(),
    )
    profile_path = tmp_path / "check.sb"
    profile_path.write_text(profile, encoding="utf-8")
    script = """
import errno
import os
import sys


def require_denied(operation, exit_code):
    try:
        operation()
    except OSError as error:
        if error.errno not in {errno.EACCES, errno.EPERM}:
            raise SystemExit(exit_code)
    else:
        raise SystemExit(exit_code)


def read_private():
    with open(sys.argv[1], "rb") as stream:
        stream.read(1)


def write_tracked():
    with open("tracked.txt", "wb") as stream:
        stream.write(b"forbidden")


with open(".cache/check/result.txt", "wb") as stream:
    stream.write(b"allowed")
require_denied(read_private, 31)
require_denied(write_tracked, 32)
require_denied(lambda: os.kill(int(sys.argv[2]), 0), 33)
try:
    child_pid = os.fork()
except OSError as error:
    if error.errno != errno.EPERM:
        raise SystemExit(35)
else:
    if child_pid == 0:
        os._exit(0)
    os.waitpid(child_pid, 0)
    raise SystemExit(34)
"""
    result = subprocess.run(
        [
            "/usr/bin/sandbox-exec", "-f", str(profile_path),
            str(executable), "-I", "-S", "-B", "-c", script,
            str(private), str(os.getpid()),
        ],
        cwd=integration,
        env={
            "HOME": str(runtime),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "TMPDIR": str(scratch),
        },
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert (cache / "result.txt").read_bytes() == b"allowed"
    assert tracked.read_text(encoding="utf-8") == "tracked\n"
    assert private.read_text(encoding="utf-8") == "private sentinel\n"


def _task6_snapshot(tmp_path):
    from agent.bestplan_source import ProtectedManifest, RepoIdentity, SourceSnapshot

    worktree = tmp_path / "repository"
    git_dir = tmp_path / "repository.git"
    worktree.mkdir()
    git_dir.mkdir()
    identity = git_dir.stat()
    repo = RepoIdentity(
        workspace=str(worktree),
        workspace_raw=os.fsencode(worktree),
        worktree=str(worktree),
        worktree_raw=os.fsencode(worktree),
        git_dir=str(git_dir),
        git_dir_raw=os.fsencode(git_dir),
        common_dir=str(git_dir),
        common_dir_raw=os.fsencode(git_dir),
        common_dir_device=identity.st_dev,
        common_dir_inode=identity.st_ino,
        object_format="sha1",
        repository_id="task6-repository",
    )
    protected = ProtectedManifest(
        index_entries=(),
        index_flags=(),
        worktree_entries=(),
        protected_paths=(),
        staged_diff_sha256="1" * 64,
        unstaged_diff_sha256="2" * 64,
        digest="3" * 64,
    )
    return SourceSnapshot(
        repo=repo,
        head_symbolic=True,
        head_ref=b"refs/heads/main",
        head_raw=b"ref: refs/heads/main\n",
        head_oid="4" * 40,
        tree_oid="5" * 40,
        protected_manifest=protected,
        capture_implementation_sha256="6" * 64,
        fingerprint="7" * 64,
    )


def _task6_command(executable: Path, identifier: str, *, timeout_seconds: int = 30):
    from agent.bestplan_contract import BoundCommand

    executable = executable.resolve()
    return BoundCommand(
        identifier=identifier,
        executable=str(executable),
        executable_sha256=_sha256(executable),
        argv=("-c", "raise SystemExit(0)", identifier),
        logical_cwd="integration",
        env=(("PYTHONHASHSEED", "0"),),
        inputs=(),
        cache=(),
        timeout_seconds=timeout_seconds,
        network_allowlist=(),
    )


def _task6_contract_bundle(
    tmp_path,
    *,
    executable: Path | None = None,
    check_timeout_seconds: int = 30,
    review_identifier: str = "review",
):
    from agent.bestplan_contract import (
        BlockingReview,
        ControllerIdentity,
        EnrolledRepository,
        Enrollment,
        LiveTarget,
        Publication,
        RollbackTarget,
        build_execution_contract,
    )

    snapshot = _task6_snapshot(tmp_path)
    executable = Path(sys.executable).resolve() if executable is None else executable
    focused = _task6_command(
        executable, "focused-tests", timeout_seconds=check_timeout_seconds,
    )
    full = _task6_command(executable, "full-tests")
    review = _task6_command(executable, review_identifier)
    activation = _task6_command(executable, "activation")
    health = _task6_command(executable, "health")
    canary = _task6_command(executable, "canary")
    rollback_command = _task6_command(executable, "rollback")
    controller = ControllerIdentity(
        repository_id=snapshot.repo.repository_id,
        controller_id="controller-n-1",
        release_oid=snapshot.head_oid,
        artifact_sha256="8" * 64,
    )
    rollback = RollbackTarget(
        repository_id=snapshot.repo.repository_id,
        selector=str((tmp_path / "release-selector").resolve()),
        service="task6-service",
        command=rollback_command,
    )
    enrollment = Enrollment(
        reference="task6-enrollment",
        enrollment_id="task6-enrollment-id",
        revision=1,
        epoch="task6-epoch",
        repository=EnrolledRepository.from_repo_identity(snapshot.repo),
        source_policy="head_only",
        capture_budget_seconds=30,
        local_ref="refs/heads/main",
        publication=Publication(
            repository_id=snapshot.repo.repository_id,
            remote_name="origin",
            push_url=str((tmp_path / "remote.git").resolve()),
            remote_ref="refs/heads/main",
            observed_oid=snapshot.head_oid,
        ),
        commands=(focused, full),
        review=BlockingReview(
            lane="smart_reviewer",
            command=review,
            blocking_severities=("critical", "high"),
        ),
        live_targets=(
            LiveTarget(
                repository_id=snapshot.repo.repository_id,
                adapter="task6-adapter",
                target_id="task6-target",
                service="task6-service",
                activation=activation,
                health=health,
                canary=canary,
                rollback=rollback,
            ),
        ),
        controller=controller,
        promotion_mode="auto_live",
    )
    plan = SimpleNamespace(slices=(SimpleNamespace(kind="implement"),))
    contract = build_execution_contract(plan, snapshot, enrollment)
    return snapshot, contract, controller, (focused, full)


def _task6_runtime(checks, tmp_path, controller):
    controller_source = tmp_path / "controller"
    cache_seed = tmp_path / "cache-seed"
    controller_source.mkdir(exist_ok=True)
    cache_seed.mkdir(exist_ok=True)
    return checks.CheckHostRuntime(
        controller_source=controller_source,
        controller=controller,
        sandbox_executable=Path("/usr/bin/sandbox-exec"),
        sandbox_executable_sha256="9" * 64,
        runtime_read_paths=(),
        cache_seed_root=cache_seed,
    )


def _task6_local_contract_bundle(checks, tmp_path):
    from agent.bestplan_contract import canonical_json
    from agent.bestplan_local import build_local_go_contract
    from agent.bestplan_sandbox import (
        _launcher_identity,
        _new_artifact_budget,
        _stable_artifact_tree_identity,
    )

    proof_root = tmp_path / "local-check-proof"
    launcher = proof_root / "venv" / "bin" / "python"
    launcher.parent.mkdir(parents=True)
    executable = proof_root / "python3.99"
    executable.write_bytes(b"test-local-python-executable\n")
    executable.chmod(0o755)
    launcher.symlink_to(executable)
    runtime_root = proof_root / "runtime"
    runtime_root.mkdir()
    pytest_module = runtime_root / "pytest" / "__init__.py"
    pytest_module.parent.mkdir()
    pytest_module.write_text("__version__ = 'test'\n", encoding="utf-8")

    snapshot, _enrolled, controller, required = _task6_contract_bundle(
        tmp_path, executable=executable,
    )
    required = tuple(
        replace(
            command,
            env=tuple(sorted((
                *command.env,
                ("__PYVENV_LAUNCHER__", str(launcher)),
            ))),
        )
        for command in required
    )
    deadline = time.monotonic() + 20.0
    budget = _new_artifact_budget(deadline)
    resolved = executable.resolve(strict=True)
    launcher_identity = _launcher_identity(launcher, resolved, budget)
    runtime_identity = _stable_artifact_tree_identity(runtime_root, budget)
    sandbox = Path("/usr/bin/sandbox-exec")
    sandbox_identity = _stable_artifact_tree_identity(sandbox, budget)
    runtime_pin = checks.PinnedRuntimePath(
        path=runtime_root,
        sha256=runtime_identity["sha256"],
    )
    runtime_kwargs = {
        "controller_source": tmp_path / "controller",
        "controller": controller,
        "sandbox_executable": sandbox,
        "sandbox_executable_sha256": sandbox_identity["sha256"],
        "runtime_read_paths": (runtime_pin,),
        "cache_seed_root": tmp_path / "cache-seed",
    }
    Path(runtime_kwargs["controller_source"]).mkdir(exist_ok=True)
    Path(runtime_kwargs["cache_seed_root"]).mkdir(exist_ok=True)
    parameters = inspect.signature(checks.CheckHostRuntime).parameters
    if {
        "controller_python_launcher", "pytest_module_path",
    } <= set(parameters):
        runtime_kwargs.update({
            "controller_python_launcher": launcher,
            "pytest_module_path": pytest_module,
        })
    runtime = checks.CheckHostRuntime(**runtime_kwargs)
    # Keep the public-path objective RED runnable before the new optional
    # dataclass fields exist. A separate API assertion requires those fields.
    if "controller_python_launcher" not in parameters:
        object.__setattr__(runtime, "controller_python_launcher", launcher)
        object.__setattr__(runtime, "pytest_module_path", pytest_module)

    runtime_body = {
        "schema": "hermes.bestplan.local-check-runtime.v1",
        "launcher": launcher_identity,
        "runtime_read_paths": [runtime_identity],
        "sandbox": sandbox_identity,
        "policy_version": checks.CHECK_SANDBOX_POLICY_VERSION,
        "pytest_module_path": str(pytest_module),
    }
    runtime_digest = hashlib.sha256(
        b"hermes.bestplan.local-check-runtime.v1\0"
        + canonical_json(runtime_body).encode("utf-8")
    ).hexdigest()
    contract = build_local_go_contract(
        snapshot=snapshot,
        controller=controller,
        commands=required,
        manifest_digest="f" * 64,
        check_runtime_digest=runtime_digest,
    )
    return snapshot, contract, controller, required, runtime, pytest_module


def _task6_integration(snapshot, contract):
    from agent.bestplan_contract import contract_digest, source_snapshot_digest
    from agent.bestplan_local import (
        LOCAL_GO_CONTRACT_SCHEMA,
        local_go_contract_digest,
    )
    from agent.bestplan_promotion import FrozenIntegration

    exact_contract_digest = (
        local_go_contract_digest(contract)
        if contract.get("schema") == LOCAL_GO_CONTRACT_SCHEMA
        else contract_digest(contract)
    )

    return FrozenIntegration(
        plan_id="task6-plan",
        approval_digest="a" * 64,
        contract_digest=exact_contract_digest,
        source_snapshot_digest=source_snapshot_digest(snapshot),
        target_ref="refs/heads/main",
        target_oid=snapshot.head_oid,
        integration_oid="b" * 40,
        tree_oid="c" * 40,
        ref_name="refs/hermes/bestplan/integrations/task6",
        candidates=(),
        receipt_digest="d" * 64,
    )


def _allow_task6_runner_preconditions(checks, monkeypatch):
    from agent import bestplan_promotion

    monkeypatch.setattr(checks.sys, "platform", "darwin")
    real_is_file = Path.is_file

    def fake_is_file(path):
        if path == Path("/usr/bin/sandbox-exec"):
            return True
        return real_is_file(path)

    monkeypatch.setattr(Path, "is_file", fake_is_file)
    monkeypatch.setattr(checks, "_assert_host_runtime", lambda runtime, **_kwargs: None)
    current_target = lambda *_args, **_kwargs: "4" * 40
    monkeypatch.setattr(bestplan_promotion, "_read_ref", current_target)
    monkeypatch.setattr(
        checks, "_read_current_target_oid", current_target, raising=False,
    )


def _assert_contract_runner_api(checks):
    signature = inspect.signature(checks.run_integration_checks)
    assert "contract" in signature.parameters, (
        "the public checker must require the validated promotion contract"
    )


def _run_task6_contract(
    checks,
    *,
    snapshot,
    integration,
    contract,
    commands,
    runtime,
    checks_root,
    deadline,
    cancel_event=None,
    precreate_checks_root=True,
):
    _assert_contract_runner_api(checks)
    checks_root = Path(checks_root)
    if (
        precreate_checks_root
        and not checks_root.exists()
        and not checks_root.is_symlink()
    ):
        checks_root.mkdir(mode=0o700)
    return checks.run_integration_checks(
        snapshot=snapshot,
        integration=integration,
        contract=contract,
        commands=commands,
        runtime=runtime,
        checks_root=checks_root,
        deadline=deadline,
        cancel_event=cancel_event,
    )


def _task6_contract_with_commands(contract, commands):
    from agent.bestplan_contract import _command_to_dict

    updated = copy.deepcopy(contract)
    updated["commands"] = [_command_to_dict(command) for command in commands]
    return updated


def _create_stub_materialized_root(kwargs) -> int:
    assert "destination" not in kwargs
    assert kwargs["destination_leaf"] == b"integration"
    os.mkdir(
        kwargs["destination_leaf"],
        mode=0o755,
        dir_fd=kwargs["parent_fd"],
    )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(
        kwargs["destination_leaf"],
        flags,
        dir_fd=kwargs["parent_fd"],
    )


def _forbid_task6_execution(checks, monkeypatch, seen):
    from agent import bestplan_promotion

    def forbidden_materialization(**kwargs):
        seen.append("materialize")
        raise AssertionError("contract rejection happened after materialization")

    def forbidden_launch(**kwargs):
        seen.append("launch")
        raise AssertionError("contract rejection happened after launch")

    monkeypatch.setattr(
        bestplan_promotion,
        "_materialize_integration_tree_at_owned_parent",
        forbidden_materialization,
    )
    monkeypatch.setattr(checks, "_launch_check_process", forbidden_launch)


def _stub_task6_execution(checks, monkeypatch, seen):
    from agent import bestplan_promotion

    def materialize(**kwargs):
        seen["materialize"] += 1
        descriptor = _create_stub_materialized_root(kwargs)
        os.close(descriptor)

    def launch(**kwargs):
        seen["launch"].append((tuple(kwargs["argv"]), dict(kwargs["environment"])))
        return SimpleNamespace(pid=7001, stdout=None, stderr=None, returncode=0)

    monkeypatch.setattr(
        bestplan_promotion,
        "_materialize_integration_tree_at_owned_parent",
        materialize,
    )
    monkeypatch.setattr(checks, "_launch_check_process", launch)
    monkeypatch.setattr(
        checks,
        "_supervise_check_process",
        lambda *args, **kwargs: checks.CapturedCheckProcess(0, b"green\n", b""),
    )


def test_public_checker_derives_exact_contract_commands_and_binds_receipt(
    tmp_path, monkeypatch,
):
    from agent.bestplan_contract import contract_digest

    checks = _checks()
    snapshot, contract, controller, required = _task6_contract_bundle(tmp_path)
    integration = _task6_integration(snapshot, contract)
    runtime = _task6_runtime(checks, tmp_path, controller)
    seen = {"materialize": 0, "launch": []}
    _allow_task6_runner_preconditions(checks, monkeypatch)
    _stub_task6_execution(checks, monkeypatch, seen)

    receipt = _run_task6_contract(
        checks,
        snapshot=snapshot,
        integration=integration,
        contract=contract,
        commands=required,
        runtime=runtime,
        checks_root=tmp_path / "checks",
        deadline=time.monotonic() + 30,
    )

    assert seen["materialize"] == 1
    assert len(seen["launch"]) == len(required)
    assert tuple(item.command_id for item in receipt.ordered_receipts) == tuple(
        item.identifier for item in required
    )
    assert contract["review"]["command"]["identifier"] not in {
        item.command_id for item in receipt.ordered_receipts
    }
    assert receipt.contract_digest == contract_digest(contract)


def test_check_host_runtime_keeps_local_proof_inputs_optional_for_legacy(
    tmp_path,
):
    checks = _checks()
    parameters = inspect.signature(checks.CheckHostRuntime).parameters

    assert parameters["controller_python_launcher"].default is None
    assert parameters["pytest_module_path"].default is None

    _snapshot, _contract, controller, _required = _task6_contract_bundle(tmp_path)
    runtime = _task6_runtime(checks, tmp_path, controller)
    assert runtime.controller_python_launcher is None
    assert runtime.pytest_module_path is None


def test_public_checker_accepts_strict_local_go_contract_and_binds_receipt(
    tmp_path, monkeypatch,
):
    from agent.bestplan_local import local_go_contract_digest

    checks = _checks()
    snapshot, contract, _controller, required, runtime, _pytest_module = (
        _task6_local_contract_bundle(checks, tmp_path)
    )
    integration = _task6_integration(snapshot, contract)
    seen = {"materialize": 0, "launch": []}
    _allow_task6_runner_preconditions(checks, monkeypatch)
    _stub_task6_execution(checks, monkeypatch, seen)

    receipt = _run_task6_contract(
        checks,
        snapshot=snapshot,
        integration=integration,
        contract=contract,
        commands=required,
        runtime=runtime,
        checks_root=tmp_path / "checks",
        deadline=time.monotonic() + 30,
    )

    assert seen["materialize"] == 1
    assert tuple(item.command_id for item in receipt.ordered_receipts) == tuple(
        item.identifier for item in required
    )
    assert receipt.contract_digest == local_go_contract_digest(contract)


def test_public_checker_requires_exact_local_runtime_proof_before_materialization(
    tmp_path, monkeypatch,
):
    checks = _checks()
    snapshot, contract, controller, required, _runtime, _pytest_module = (
        _task6_local_contract_bundle(checks, tmp_path)
    )
    integration = _task6_integration(snapshot, contract)
    legacy_runtime = _task6_runtime(checks, tmp_path, controller)
    seen = []
    _allow_task6_runner_preconditions(checks, monkeypatch)
    _forbid_task6_execution(checks, monkeypatch, seen)

    with pytest.raises(
        checks.CheckValidationError,
        match=r"(?i)local.*runtime|runtime.*proof|launcher|pytest",
    ):
        _run_task6_contract(
            checks,
            snapshot=snapshot,
            integration=integration,
            contract=contract,
            commands=required,
            runtime=legacy_runtime,
            checks_root=tmp_path / "checks",
            deadline=time.monotonic() + 30,
        )

    assert seen == []


def test_public_checker_recomputes_local_runtime_identity_before_materialization(
    tmp_path, monkeypatch,
):
    checks = _checks()
    snapshot, contract, _controller, required, runtime, pytest_module = (
        _task6_local_contract_bundle(checks, tmp_path)
    )
    integration = _task6_integration(snapshot, contract)
    pytest_module.write_text("__version__ = 'changed'\n", encoding="utf-8")
    seen = []
    _allow_task6_runner_preconditions(checks, monkeypatch)
    _forbid_task6_execution(checks, monkeypatch, seen)

    with pytest.raises(
        checks.CheckProofStale,
        match=r"(?i)local.*runtime|runtime.*digest|dependency.*changed",
    ):
        _run_task6_contract(
            checks,
            snapshot=snapshot,
            integration=integration,
            contract=contract,
            commands=required,
            runtime=runtime,
            checks_root=tmp_path / "checks",
            deadline=time.monotonic() + 30,
        )

    assert seen == []


@pytest.mark.parametrize("mutation", ("subset", "substitute"))
def test_public_checker_rejects_nonexact_contract_command_set_before_materialization(
    tmp_path, monkeypatch, mutation,
):
    checks = _checks()
    snapshot, contract, controller, required = _task6_contract_bundle(tmp_path)
    integration = _task6_integration(snapshot, contract)
    runtime = _task6_runtime(checks, tmp_path, controller)
    supplied = required[:-1] if mutation == "subset" else (
        replace(required[0], argv=required[0].argv + ("--substituted",)),
        *required[1:],
    )
    seen = []
    _allow_task6_runner_preconditions(checks, monkeypatch)
    _forbid_task6_execution(checks, monkeypatch, seen)

    with pytest.raises(checks.CheckError, match="contract|command"):
        _run_task6_contract(
            checks,
            snapshot=snapshot,
            integration=integration,
            contract=contract,
            commands=supplied,
            runtime=runtime,
            checks_root=tmp_path / "checks",
            deadline=time.monotonic() + 30,
        )
    assert seen == []


def test_public_checker_rejects_controller_not_bound_to_contract_before_materialization(
    tmp_path, monkeypatch,
):
    checks = _checks()
    snapshot, contract, controller, required = _task6_contract_bundle(tmp_path)
    integration = _task6_integration(snapshot, contract)
    wrong_controller = replace(
        controller,
        controller_id="controller-substitute",
        artifact_sha256="e" * 64,
    )
    runtime = _task6_runtime(checks, tmp_path, wrong_controller)
    seen = []
    _allow_task6_runner_preconditions(checks, monkeypatch)
    _forbid_task6_execution(checks, monkeypatch, seen)

    with pytest.raises(checks.CheckError, match="controller"):
        _run_task6_contract(
            checks,
            snapshot=snapshot,
            integration=integration,
            contract=contract,
            commands=required,
            runtime=runtime,
            checks_root=tmp_path / "checks",
            deadline=time.monotonic() + 30,
        )
    assert seen == []


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("repository_workspace", "repository"),
        ("repository_worktree", "repository"),
        ("repository_git_dir", "repository"),
        ("repository_common_dir", "repository"),
        ("repository_common_dir_device", "repository"),
        ("repository_common_dir_inode", "repository"),
        ("base_and_local_main_oid", "source|base|local"),
        ("tree_oid", "source|tree"),
        ("source_digest", "source"),
        ("protected_digest", "protected"),
        ("snapshot_digest", "snapshot"),
    ),
)
def test_public_checker_rejects_contract_source_identity_drift_before_materialization(
    tmp_path, monkeypatch, mutation, message,
):
    from agent.bestplan_contract import contract_digest

    checks = _checks()
    snapshot, contract, controller, required = _task6_contract_bundle(tmp_path)
    altered = copy.deepcopy(contract)
    integration = _task6_integration(snapshot, altered)
    if mutation.startswith("repository_"):
        field = mutation.removeprefix("repository_")
        if field in {"workspace", "worktree", "git_dir", "common_dir"}:
            replacement = str((tmp_path / f"different-{field}").resolve())
            altered["repository"][field] = replacement
            altered["repository"][f"{field}_raw_b64"] = base64.b64encode(
                os.fsencode(replacement),
            ).decode("ascii")
        else:
            altered["repository"][field] += 1
    elif mutation == "base_and_local_main_oid":
        altered["source"]["base_oid"] = "e" * 40
        altered["source"]["local_main_oid"] = "e" * 40
    elif mutation == "tree_oid":
        altered["source"]["tree_oid"] = "f" * 40
    elif mutation == "source_digest":
        altered["source"]["source_digest"] = "8" * 64
    elif mutation == "protected_digest":
        altered["source"]["protected_digest"] = "9" * 64
    else:
        altered["source"]["snapshot_digest"] = "a" * 64
        integration = replace(
            integration,
            source_snapshot_digest=altered["source"]["snapshot_digest"],
        )
    integration = replace(integration, contract_digest=contract_digest(altered))
    runtime = _task6_runtime(checks, tmp_path, controller)
    seen = []
    _allow_task6_runner_preconditions(checks, monkeypatch)
    _forbid_task6_execution(checks, monkeypatch, seen)

    with pytest.raises(checks.CheckError, match=message):
        _run_task6_contract(
            checks,
            snapshot=snapshot,
            integration=integration,
            contract=altered,
            commands=required,
            runtime=runtime,
            checks_root=tmp_path / "checks",
            deadline=time.monotonic() + 30,
        )
    assert seen == []


def test_public_checker_accepts_current_target_advancement_from_source_snapshot(
    tmp_path, monkeypatch,
):
    from agent import bestplan_promotion

    checks = _checks()
    snapshot, contract, controller, required = _task6_contract_bundle(tmp_path)
    integration = replace(
        _task6_integration(snapshot, contract),
        target_oid="9" * 40,
    )
    runtime = _task6_runtime(checks, tmp_path, controller)
    seen = {"materialize": 0, "launch": []}
    _allow_task6_runner_preconditions(checks, monkeypatch)
    current_target = lambda *_args, **_kwargs: integration.target_oid
    monkeypatch.setattr(bestplan_promotion, "_read_ref", current_target)
    monkeypatch.setattr(
        checks, "_read_current_target_oid", current_target, raising=False,
    )
    _stub_task6_execution(checks, monkeypatch, seen)

    receipt = _run_task6_contract(
        checks,
        snapshot=snapshot,
        integration=integration,
        contract=contract,
        commands=required,
        runtime=runtime,
        checks_root=tmp_path / "checks",
        deadline=time.monotonic() + 30,
    )

    assert seen["materialize"] == 1
    assert len(receipt.ordered_receipts) == len(required)


def test_public_checker_rejects_changed_current_target_before_materialization(
    tmp_path, monkeypatch,
):
    from agent import bestplan_promotion

    checks = _checks()
    snapshot, contract, controller, required = _task6_contract_bundle(tmp_path)
    integration = replace(
        _task6_integration(snapshot, contract),
        target_oid="9" * 40,
    )
    runtime = _task6_runtime(checks, tmp_path, controller)
    seen = []
    _allow_task6_runner_preconditions(checks, monkeypatch)
    changed_target = lambda *_args, **_kwargs: "a" * 40
    monkeypatch.setattr(bestplan_promotion, "_read_ref", changed_target)
    monkeypatch.setattr(
        checks, "_read_current_target_oid", changed_target, raising=False,
    )
    _forbid_task6_execution(checks, monkeypatch, seen)

    with pytest.raises(checks.CheckProofStale, match="target|ref"):
        _run_task6_contract(
            checks,
            snapshot=snapshot,
            integration=integration,
            contract=contract,
            commands=required,
            runtime=runtime,
            checks_root=tmp_path / "checks",
            deadline=time.monotonic() + 30,
        )
    assert seen == []


def test_nested_cache_symlink_is_rejected_without_copying_target_bytes(tmp_path):
    from agent.bestplan_contract import PinnedInput

    checks = _checks()
    seed_root = tmp_path / "seed"
    source = seed_root / ".cache" / "check"
    source.mkdir(parents=True)
    sentinel = tmp_path / "credential-sentinel"
    sentinel.write_bytes(b"must-not-cross-cache-boundary")
    (source / "nested-link").symlink_to(sentinel)
    spec = PinnedInput(
        ".cache/check", checks.pinned_path_sha256(source),
    )
    target_root = tmp_path / "integration"
    target_root.mkdir()

    with pytest.raises(checks.CheckProofStale, match="symlink|cache"):
        checks._copy_cache_seed(
            seed_root,
            target_root,
            spec,
            deadline=time.monotonic() + 5,
        )

    copied = target_root / ".cache" / "check" / "nested-link"
    assert not copied.exists()
    assert not any(
        path.is_file() and path.read_bytes() == sentinel.read_bytes()
        for path in target_root.rglob("*")
    )


@pytest.mark.parametrize("swap_kind", ("source-root", "source-ancestor"))
def test_cache_copy_materializes_only_captured_bytes_across_source_swap(
    tmp_path, monkeypatch, swap_kind,
):
    from agent.bestplan_contract import PinnedInput

    checks = _checks()
    seed_root = tmp_path / "seed"
    source = seed_root / ".cache" / "check"
    source.mkdir(parents=True)
    payload = source / "payload.bin"
    payload.write_bytes(b"approved-cache")
    spec = PinnedInput(
        ".cache/check", checks.pinned_path_sha256(source),
    )
    external_parent = tmp_path / "external-cache-parent"
    external_source = external_parent / "check"
    external_source.mkdir(parents=True)
    external_sentinel = external_source / "payload.bin"
    external_sentinel.write_bytes(b"external-sentinel")
    target_root = tmp_path / "integration"
    target_root.mkdir()
    real_capture = checks._capture_check_tree
    real_open = checks.os.open
    swapped = False
    external_reads = []

    def swap_after_capture(root, *, deadline):
        nonlocal swapped
        captured = real_capture(root, deadline=deadline)
        if Path(root) == source and not swapped:
            if swap_kind == "source-root":
                source.rename(seed_root / "approved-check")
                source.symlink_to(external_source, target_is_directory=True)
            else:
                source.parent.rename(seed_root / "approved-cache-parent")
                source.parent.symlink_to(external_parent, target_is_directory=True)
            swapped = True
        return captured

    def forbid_external_open(path, flags, *args, **kwargs):
        try:
            resolved = Path(path).resolve(strict=False)
        except (OSError, TypeError, ValueError):
            resolved = None
        if resolved is not None and (
            resolved == external_parent or external_parent in resolved.parents
        ):
            external_reads.append(resolved)
            raise AssertionError("cache materialization read swapped external bytes")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(checks, "_capture_check_tree", swap_after_capture)
    monkeypatch.setattr(checks.os, "open", forbid_external_open)

    copied = checks._copy_cache_seed(
        seed_root,
        target_root,
        spec,
        deadline=time.monotonic() + 5,
    )

    assert swapped is True
    assert external_reads == []
    assert (copied / "payload.bin").read_bytes() == b"approved-cache"
    assert not any(
        item.is_file() and item.read_bytes() == b"external-sentinel"
        for item in target_root.rglob("*")
    )


def test_launch_rejects_executable_digest_mismatch_before_popen(tmp_path, monkeypatch):
    checks = _checks()
    executable = tmp_path / "checker"
    executable.write_bytes(b"approved-checker")
    executable.chmod(0o755)
    expected = _sha256(executable)
    executable.write_bytes(b"substituted-checker")
    cwd = tmp_path / "integration"
    cwd.mkdir()
    profile = tmp_path / "check.sb"
    profile.write_text("(version 1)\n(deny default)\n", encoding="utf-8")
    popen_calls = []
    monkeypatch.setattr(
        checks.subprocess,
        "Popen",
        lambda *args, **kwargs: popen_calls.append((args, kwargs)),
    )
    signature = inspect.signature(checks._launch_check_process)
    assert "executable_sha256" in signature.parameters, (
        "the launch seam must bind the executable digest it is about to execute"
    )

    with pytest.raises(checks.CheckProofStale, match="executable.*digest"):
        checks._launch_check_process(
            executable=executable,
            executable_sha256=expected,
            argv=(),
            cwd=cwd,
            environment={"LANG": "C"},
            profile_path=profile,
        )
    assert popen_calls == []


def test_public_checker_rejects_executable_swap_before_process_launch(tmp_path, monkeypatch):
    checks = _checks()
    executable = tmp_path / "checker"
    executable.write_bytes(b"approved-checker")
    executable.chmod(0o755)
    snapshot, contract, controller, required = _task6_contract_bundle(
        tmp_path, executable=executable,
    )
    integration = _task6_integration(snapshot, contract)
    runtime = _task6_runtime(checks, tmp_path, controller)
    _allow_task6_runner_preconditions(checks, monkeypatch)
    from agent import bestplan_promotion

    def materialize(**kwargs):
        descriptor = _create_stub_materialized_root(kwargs)
        os.close(descriptor)

    monkeypatch.setattr(
        bestplan_promotion,
        "_materialize_integration_tree_at_owned_parent",
        materialize,
    )
    monkeypatch.setattr(
        checks,
        "_supervise_check_process",
        lambda *args, **kwargs: checks.CapturedCheckProcess(0, b"", b""),
    )
    real_profile = checks._check_profile_text
    swapped = False

    def swap_after_initial_pin(**kwargs):
        nonlocal swapped
        profile = real_profile(**kwargs)
        if not swapped:
            executable.write_bytes(b"substituted-after-initial-pin")
            swapped = True
        return profile

    monkeypatch.setattr(checks, "_check_profile_text", swap_after_initial_pin)
    popen_calls = []

    def fake_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        return SimpleNamespace(pid=7002, stdout=None, stderr=None, returncode=0)

    monkeypatch.setattr(checks.subprocess, "Popen", fake_popen)

    with pytest.raises(checks.CheckProofStale, match="executable"):
        _run_task6_contract(
            checks,
            snapshot=snapshot,
            integration=integration,
            contract=contract,
            commands=required,
            runtime=runtime,
            checks_root=tmp_path / "checks",
            deadline=time.monotonic() + 30,
        )
    assert popen_calls == []


@pytest.mark.parametrize("deadline_kind", ("nan", "infinite", "overlong"))
def test_public_checker_rejects_invalid_global_deadline_before_materialization(
    tmp_path, monkeypatch, deadline_kind,
):
    checks = _checks()
    snapshot, contract, controller, required = _task6_contract_bundle(tmp_path)
    integration = _task6_integration(snapshot, contract)
    runtime = _task6_runtime(checks, tmp_path, controller)
    deadline = {
        "nan": float("nan"),
        "infinite": float("inf"),
        "overlong": time.monotonic() + checks.MAX_CHECK_TIMEOUT_SECONDS + 1,
    }[deadline_kind]
    seen = []
    _allow_task6_runner_preconditions(checks, monkeypatch)
    _forbid_task6_execution(checks, monkeypatch, seen)

    with pytest.raises(checks.CheckValidationError, match="deadline"):
        _run_task6_contract(
            checks,
            snapshot=snapshot,
            integration=integration,
            contract=contract,
            commands=required,
            runtime=runtime,
            checks_root=tmp_path / "checks",
            deadline=deadline,
        )
    assert seen == []


def test_public_checker_rejects_overlong_contract_timeout_before_materialization(
    tmp_path, monkeypatch,
):
    checks = _checks()
    snapshot, contract, controller, required = _task6_contract_bundle(
        tmp_path, check_timeout_seconds=checks.MAX_CHECK_TIMEOUT_SECONDS + 1,
    )
    integration = _task6_integration(snapshot, contract)
    runtime = _task6_runtime(checks, tmp_path, controller)
    seen = []
    _allow_task6_runner_preconditions(checks, monkeypatch)
    _forbid_task6_execution(checks, monkeypatch, seen)

    with pytest.raises(checks.CheckValidationError, match="timeout"):
        _run_task6_contract(
            checks,
            snapshot=snapshot,
            integration=integration,
            contract=contract,
            commands=required,
            runtime=runtime,
            checks_root=tmp_path / "checks",
            deadline=time.monotonic() + 30,
        )
    assert seen == []


def test_public_checker_excludes_review_when_its_identifier_matches_a_check(
    tmp_path, monkeypatch,
):
    checks = _checks()
    snapshot, contract, controller, required = _task6_contract_bundle(
        tmp_path, review_identifier="focused-tests",
    )
    integration = _task6_integration(snapshot, contract)
    runtime = _task6_runtime(checks, tmp_path, controller)
    seen = {"materialize": 0, "launch": []}
    _allow_task6_runner_preconditions(checks, monkeypatch)
    _stub_task6_execution(checks, monkeypatch, seen)

    receipt = _run_task6_contract(
        checks,
        snapshot=snapshot,
        integration=integration,
        contract=contract,
        commands=required,
        runtime=runtime,
        checks_root=tmp_path / "checks",
        deadline=time.monotonic() + 30,
    )

    assert seen["materialize"] == 1
    assert tuple(item.command_id for item in receipt.ordered_receipts) == (
        "focused-tests",
        "full-tests",
    )


@pytest.mark.parametrize("root_kind", ("exact", "symlink-alias"))
def test_checks_root_alias_of_trusted_repo_is_rejected_before_mkdir(
    tmp_path, monkeypatch, root_kind,
):
    checks = _checks()
    snapshot, contract, controller, required = _task6_contract_bundle(tmp_path)
    integration = _task6_integration(snapshot, contract)
    runtime = _task6_runtime(checks, tmp_path, controller)
    trusted_root = Path(snapshot.repo.worktree)
    if root_kind == "exact":
        checks_root = trusted_root
    else:
        checks_root = tmp_path / "checks-alias"
        checks_root.symlink_to(trusted_root, target_is_directory=True)
    seen = []
    _allow_task6_runner_preconditions(checks, monkeypatch)
    _forbid_task6_execution(checks, monkeypatch, seen)
    real_mkdir = Path.mkdir
    root_mkdir_calls = []

    def tracked_mkdir(path, *args, **kwargs):
        if path == checks_root.absolute():
            root_mkdir_calls.append(path)
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", tracked_mkdir)

    with pytest.raises(checks.CheckValidationError, match="root|overlap|alias"):
        _run_task6_contract(
            checks,
            snapshot=snapshot,
            integration=integration,
            contract=contract,
            commands=required,
            runtime=runtime,
            checks_root=checks_root,
            deadline=time.monotonic() + 30,
        )
    assert root_mkdir_calls == []
    assert seen == []


def test_checks_root_must_already_exist_without_creating_any_component(
    tmp_path, monkeypatch,
):
    checks = _checks()
    snapshot, contract, controller, required = _task6_contract_bundle(tmp_path)
    integration = _task6_integration(snapshot, contract)
    runtime = _task6_runtime(checks, tmp_path, controller)
    checks_root = tmp_path / "missing-checks-root"
    seen = []
    forbidden_mkdir = []
    _allow_task6_runner_preconditions(checks, monkeypatch)
    _forbid_task6_execution(checks, monkeypatch, seen)
    real_mkdir = Path.mkdir

    def reject_root_mkdir(path, *args, **kwargs):
        candidate = Path(path).absolute()
        if candidate == checks_root or checks_root in candidate.parents:
            forbidden_mkdir.append(candidate)
            raise AssertionError("checker attempted to create its host-owned root")
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", reject_root_mkdir)

    with pytest.raises(checks.CheckValidationError, match="root|exist"):
        _run_task6_contract(
            checks,
            snapshot=snapshot,
            integration=integration,
            contract=contract,
            commands=required,
            runtime=runtime,
            checks_root=checks_root,
            deadline=time.monotonic() + 30,
            precreate_checks_root=False,
        )
    assert forbidden_mkdir == []
    assert not checks_root.exists()
    assert seen == []


def test_checks_root_ancestor_swap_is_rejected_without_forbidden_mkdir(
    tmp_path, monkeypatch,
):
    checks = _checks()
    snapshot, contract, controller, required = _task6_contract_bundle(tmp_path)
    integration = _task6_integration(snapshot, contract)
    runtime = _task6_runtime(checks, tmp_path, controller)
    host_parent = tmp_path / "host-owned-parent"
    checks_root = host_parent / "checks"
    checks_root.mkdir(parents=True, mode=0o700)
    trusted_root = Path(snapshot.repo.worktree)
    (trusted_root / "checks").mkdir(mode=0o700)
    displaced_parent = tmp_path / "displaced-host-owned-parent"
    seen = []
    forbidden_mkdir = []
    swapped = False
    _allow_task6_runner_preconditions(checks, monkeypatch)
    _forbid_task6_execution(checks, monkeypatch, seen)
    real_mkdir = checks.os.mkdir

    def swap_ancestor_before_attempt_mkdir(path, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        raw = os.fsdecode(path)
        if Path(raw).name.startswith("bestplan-check-") and not swapped:
            host_parent.rename(displaced_parent)
            host_parent.symlink_to(trusted_root, target_is_directory=True)
            swapped = True
            if dir_fd is None:
                forbidden_mkdir.append(Path(raw))
                raise AssertionError("attempt mkdir followed a swapped root ancestor")
        if dir_fd is None:
            return real_mkdir(path, mode)
        return real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(checks.os, "mkdir", swap_ancestor_before_attempt_mkdir)

    with pytest.raises(
        checks.CheckValidationError,
        match="root|alias|changed|stable",
    ):
        _run_task6_contract(
            checks,
            snapshot=snapshot,
            integration=integration,
            contract=contract,
            commands=required,
            runtime=runtime,
            checks_root=checks_root,
            deadline=time.monotonic() + 30,
        )

    assert swapped is True
    assert forbidden_mkdir == []
    assert seen == []


def test_postverification_checks_root_swap_cannot_redirect_child_writes(
    tmp_path, monkeypatch,
):
    checks = _checks()
    snapshot, contract, controller, required = _task6_contract_bundle(tmp_path)
    integration = _task6_integration(snapshot, contract)
    runtime = _task6_runtime(checks, tmp_path, controller)
    host_parent = tmp_path / "host-owned-parent"
    checks_root = host_parent / "checks"
    checks_root.mkdir(parents=True, mode=0o700)
    displaced_parent = tmp_path / "displaced-host-owned-parent"
    forbidden_root = Path(snapshot.repo.worktree)
    forbidden_checks = forbidden_root / "checks"
    forbidden_checks.mkdir(mode=0o700)
    forbidden_attempt = None
    forbidden_write_attempts = []
    seen = {"materialize": 0, "launch": []}
    real_create = checks._create_owned_attempt
    real_path_mkdir = Path.mkdir
    _allow_task6_runner_preconditions(checks, monkeypatch)
    _stub_task6_execution(checks, monkeypatch, seen)

    def create_then_swap(root, *, deadline):
        nonlocal forbidden_attempt
        attempt = real_create(root, deadline=deadline)
        prepared_forbidden = forbidden_checks / attempt.leaf
        real_path_mkdir(prepared_forbidden, mode=0o700)
        forbidden_attempt = prepared_forbidden
        host_parent.rename(displaced_parent)
        host_parent.symlink_to(forbidden_root, target_is_directory=True)
        return attempt

    def reject_forbidden_child_mkdir(path, *args, **kwargs):
        resolved = Path(path).resolve(strict=False)
        if forbidden_attempt is not None and (
            resolved == forbidden_attempt
            or forbidden_attempt in resolved.parents
        ):
            forbidden_write_attempts.append(resolved)
            raise AssertionError("check child write followed a swapped ancestor")
        return real_path_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(checks, "_create_owned_attempt", create_then_swap)
    monkeypatch.setattr(Path, "mkdir", reject_forbidden_child_mkdir)

    try:
        _run_task6_contract(
            checks,
            snapshot=snapshot,
            integration=integration,
            contract=contract,
            commands=required,
            runtime=runtime,
            checks_root=checks_root,
            deadline=time.monotonic() + 30,
        )
    except checks.CheckValidationError:
        pass

    assert forbidden_attempt is not None
    assert forbidden_write_attempts == []
    assert tuple(forbidden_attempt.iterdir()) == ()


def test_checker_rejects_escaping_cwd_symlink_ancestor_before_launch(
    tmp_path, monkeypatch,
):
    from agent import bestplan_promotion

    checks = _checks()
    snapshot, contract, controller, required = _task6_contract_bundle(tmp_path)
    escaping = replace(
        required[0],
        logical_cwd="integration/escape/work",
    )
    required = (escaping, required[1])
    contract = _task6_contract_with_commands(contract, required)
    integration = _task6_integration(snapshot, contract)
    runtime = _task6_runtime(checks, tmp_path, controller)
    outside = tmp_path / "outside-integration"
    (outside / "work").mkdir(parents=True)
    launch_calls = []
    _allow_task6_runner_preconditions(checks, monkeypatch)

    def materialize_with_escaping_ancestor(**kwargs):
        descriptor = _create_stub_materialized_root(kwargs)
        try:
            os.symlink(
                os.fsencode(outside),
                b"escape",
                dir_fd=descriptor,
            )
        finally:
            os.close(descriptor)

    def forbidden_launch(**kwargs):
        launch_calls.append(Path(kwargs["cwd"]))
        raise AssertionError("escaping logical cwd reached process launch")

    monkeypatch.setattr(
        bestplan_promotion,
        "_materialize_integration_tree_at_owned_parent",
        materialize_with_escaping_ancestor,
    )
    monkeypatch.setattr(checks, "_launch_check_process", forbidden_launch)

    with pytest.raises(
        (checks.CheckValidationError, checks.CheckProofStale),
        match="cwd|symlink|integration|contain",
    ):
        _run_task6_contract(
            checks,
            snapshot=snapshot,
            integration=integration,
            contract=contract,
            commands=required,
            runtime=runtime,
            checks_root=tmp_path / "checks",
            deadline=time.monotonic() + 30,
        )

    assert launch_calls == []


def test_each_command_gets_distinct_runtime_scratch_and_only_its_exact_cache(
    tmp_path, monkeypatch,
):
    from agent.bestplan_contract import PinnedInput

    checks = _checks()
    snapshot, contract, controller, required = _task6_contract_bundle(tmp_path)
    command_a = replace(
        required[0],
        cache=(PinnedInput(".cache/a", checks.EMPTY_CACHE_SHA256),),
    )
    command_b = replace(
        required[1],
        cache=(PinnedInput(".cache/b", checks.EMPTY_CACHE_SHA256),),
    )
    required = (command_a, command_b)
    contract = _task6_contract_with_commands(contract, required)
    integration = _task6_integration(snapshot, contract)
    runtime = _task6_runtime(checks, tmp_path, controller)
    seen = {"materialize": 0, "launch": []}
    profiles = []
    visible_caches = []
    _allow_task6_runner_preconditions(checks, monkeypatch)
    _stub_task6_execution(checks, monkeypatch, seen)
    real_profile = checks._check_profile_text

    def capture_profile(**kwargs):
        profiles.append(kwargs)
        return real_profile(**kwargs)

    def launch_with_visibility(**kwargs):
        identifier = kwargs["argv"][-1]
        integration_root = Path(kwargs["cwd"])
        visible_caches.append((
            identifier,
            (integration_root / ".cache" / "a").exists(),
            (integration_root / ".cache" / "b").exists(),
        ))
        seen["launch"].append((
            tuple(kwargs["argv"]),
            dict(kwargs["environment"]),
        ))
        return SimpleNamespace(pid=7003, stdout=None, stderr=None, returncode=0)

    monkeypatch.setattr(checks, "_check_profile_text", capture_profile)
    monkeypatch.setattr(checks, "_launch_check_process", launch_with_visibility)

    _run_task6_contract(
        checks,
        snapshot=snapshot,
        integration=integration,
        contract=contract,
        commands=required,
        runtime=runtime,
        checks_root=tmp_path / "checks",
        deadline=time.monotonic() + 30,
    )

    assert visible_caches == [
        ("focused-tests", True, False),
        ("full-tests", False, True),
    ]
    cache_grants = [
        tuple(
            Path(path).relative_to(profile["integration_root"]).as_posix()
            for path in profile["cache_roots"]
        )
        for profile in profiles
    ]
    assert cache_grants == [(".cache/a",), (".cache/b",)]
    assert len({profile["runtime_root"] for profile in profiles}) == 2
    assert len({profile["scratch_root"] for profile in profiles}) == 2
    assert len({launch[1]["HOME"] for launch in seen["launch"]}) == 2
    assert len({launch[1]["TMPDIR"] for launch in seen["launch"]}) == 2


def test_logically_identical_retries_have_identical_policy_and_receipt_digests(
    tmp_path, monkeypatch,
):
    checks = _checks()
    snapshot, contract, controller, required = _task6_contract_bundle(tmp_path)
    integration = _task6_integration(snapshot, contract)
    runtime = _task6_runtime(checks, tmp_path, controller)
    seen = {"materialize": 0, "launch": []}
    _allow_task6_runner_preconditions(checks, monkeypatch)
    _stub_task6_execution(checks, monkeypatch, seen)

    receipts = [
        _run_task6_contract(
            checks,
            snapshot=snapshot,
            integration=integration,
            contract=contract,
            commands=required,
            runtime=runtime,
            checks_root=tmp_path / root_name,
            deadline=time.monotonic() + 30,
        )
        for root_name in ("checks-first", "checks-retry")
    ]

    assert tuple(
        item.policy_digest for item in receipts[0].ordered_receipts
    ) == tuple(
        item.policy_digest for item in receipts[1].ordered_receipts
    )
    assert tuple(
        item.receipt_digest for item in receipts[0].ordered_receipts
    ) == tuple(
        item.receipt_digest for item in receipts[1].ordered_receipts
    )
    assert receipts[0].receipt_digest == receipts[1].receipt_digest


@pytest.mark.parametrize("field", ("cwd", "input", "cache"))
@pytest.mark.parametrize("component", (".git", ".GiT"))
def test_git_metadata_component_alias_is_rejected_before_materialization(
    tmp_path, monkeypatch, field, component,
):
    from agent.bestplan_contract import PinnedInput

    checks = _checks()
    snapshot, contract, controller, required = _task6_contract_bundle(tmp_path)
    if field == "cwd":
        bad = replace(required[0], logical_cwd=f"integration/{component}/work")
    elif field == "input":
        bad = replace(
            required[0],
            inputs=(PinnedInput(f"{component}/config", "f" * 64),),
        )
    else:
        bad = replace(
            required[0],
            cache=(PinnedInput(
                f"{component}/cache",
                checks.EMPTY_CACHE_SHA256,
            ),),
        )
    required = (bad, required[1])
    contract = _task6_contract_with_commands(contract, required)
    integration = _task6_integration(snapshot, contract)
    runtime = _task6_runtime(checks, tmp_path, controller)
    seen = []
    _allow_task6_runner_preconditions(checks, monkeypatch)
    _forbid_task6_execution(checks, monkeypatch, seen)

    with pytest.raises(checks.CheckValidationError, match=r"(?i)git|metadata"):
        _run_task6_contract(
            checks,
            snapshot=snapshot,
            integration=integration,
            contract=contract,
            commands=required,
            runtime=runtime,
            checks_root=tmp_path / "checks",
            deadline=time.monotonic() + 30,
        )
    assert seen == []


def test_global_deadline_reserves_distinct_term_and_kill_reap_intervals(
    tmp_path, monkeypatch,
):
    checks = _checks()
    snapshot, contract, controller, required = _task6_contract_bundle(tmp_path)
    integration = _task6_integration(snapshot, contract)
    reap_grace = 0.2
    runtime = replace(
        _task6_runtime(checks, tmp_path, controller),
        reap_grace_seconds=reap_grace,
    )
    seen = {"materialize": 0, "launch": []}
    observed = []
    _allow_task6_runner_preconditions(checks, monkeypatch)
    _stub_task6_execution(checks, monkeypatch, seen)

    def capture_supervision(
        process,
        *,
        deadline,
        cancel_event,
        max_output_bytes,
        reap_grace_seconds,
        cleanup_deadline,
    ):
        observed.append((deadline, cleanup_deadline, reap_grace_seconds))
        return checks.CapturedCheckProcess(0, b"green\n", b"")

    monkeypatch.setattr(
        checks,
        "_supervise_check_process",
        capture_supervision,
    )
    global_deadline = time.monotonic() + 5

    _run_task6_contract(
        checks,
        snapshot=snapshot,
        integration=integration,
        contract=contract,
        commands=required,
        runtime=runtime,
        checks_root=tmp_path / "checks",
        deadline=global_deadline,
    )

    assert len(observed) == len(required)
    for run_deadline, hard_deadline, observed_grace in observed:
        assert hard_deadline == global_deadline
        assert observed_grace == reap_grace
        assert run_deadline <= hard_deadline - (2 * observed_grace)


def test_runner_propagates_one_absolute_deadline_to_scan_and_teardown_helpers(
    tmp_path, monkeypatch,
):
    from agent.bestplan_contract import PinnedInput

    checks = _checks()
    snapshot, contract, controller, required = _task6_contract_bundle(tmp_path)
    required = (
        replace(
            required[0],
            cache=(PinnedInput(".cache/check", checks.EMPTY_CACHE_SHA256),),
        ),
        required[1],
    )
    contract = _task6_contract_with_commands(contract, required)
    integration = _task6_integration(snapshot, contract)
    runtime = _task6_runtime(checks, tmp_path, controller)
    absolute_deadline = time.monotonic() + 30
    observed = {"runtime": [], "cache": [], "cleanup": []}
    seen = {"materialize": 0, "launch": []}
    _allow_task6_runner_preconditions(checks, monkeypatch)
    _stub_task6_execution(checks, monkeypatch, seen)

    def capture_runtime(_runtime, *, deadline=None):
        observed["runtime"].append(deadline)

    def capture_cache(
        seed_root,
        target_root,
        spec,
        *,
        deadline=None,
        cancel_event=None,
        target_root_fd=None,
    ):
        observed["cache"].append((deadline, cancel_event))
        assert isinstance(target_root_fd, int)
        target = Path(target_root).joinpath(*Path(spec.path).parts)
        target.mkdir(parents=True)
        return target

    def capture_cleanup(path, *, deadline=None, cancel_event=None):
        observed["cleanup"].append((deadline, cancel_event))

    monkeypatch.setattr(checks, "_assert_host_runtime", capture_runtime)
    monkeypatch.setattr(checks, "_copy_cache_seed", capture_cache)
    monkeypatch.setattr(checks, "_cleanup_owned_attempt", capture_cleanup)

    _run_task6_contract(
        checks,
        snapshot=snapshot,
        integration=integration,
        contract=contract,
        commands=required,
        runtime=runtime,
        checks_root=tmp_path / "checks",
        deadline=absolute_deadline,
    )

    assert observed["runtime"]
    assert observed["cache"]
    assert observed["cleanup"]
    assert set(observed["runtime"]) == {absolute_deadline}
    assert observed["cache"] == [(absolute_deadline, None)]
    assert observed["cleanup"] == [(absolute_deadline, None)]


def test_runner_passes_same_cancel_event_to_integration_materialization(
    tmp_path, monkeypatch,
):
    from agent import bestplan_promotion

    checks = _checks()
    snapshot, contract, controller, required = _task6_contract_bundle(tmp_path)
    integration = _task6_integration(snapshot, contract)
    runtime = _task6_runtime(checks, tmp_path, controller)
    cancellation = threading.Event()
    absolute_deadline = time.monotonic() + 30
    observed = []
    _allow_task6_runner_preconditions(checks, monkeypatch)

    def stop_after_control_capture(**kwargs):
        observed.append((kwargs.get("deadline"), kwargs.get("cancel_event")))
        raise checks.CheckExecutionError(
            "stop after materialization control capture"
        )

    monkeypatch.setattr(
        bestplan_promotion,
        "_materialize_integration_tree_at_owned_parent",
        stop_after_control_capture,
    )

    with pytest.raises(
        checks.CheckExecutionError,
        match="materialization control capture",
    ):
        _run_task6_contract(
            checks,
            snapshot=snapshot,
            integration=integration,
            contract=contract,
            commands=required,
            runtime=runtime,
            checks_root=tmp_path / "checks",
            deadline=absolute_deadline,
            cancel_event=cancellation,
        )

    assert observed == [(absolute_deadline, cancellation)]


def test_runner_materializes_through_retained_attempt_fd_identity_and_leaf(
    tmp_path, monkeypatch,
):
    from agent import bestplan_promotion

    checks = _checks()
    snapshot, contract, controller, required = _task6_contract_bundle(tmp_path)
    integration = _task6_integration(snapshot, contract)
    runtime = _task6_runtime(checks, tmp_path, controller)
    cancellation = threading.Event()
    absolute_deadline = time.monotonic() + 30
    observed = []
    _allow_task6_runner_preconditions(checks, monkeypatch)

    def capture_owned_parent(**kwargs):
        parent_fd = kwargs["parent_fd"]
        opened = os.fstat(parent_fd)
        observed.append({
            **kwargs,
            "opened_identity": (
                opened.st_dev,
                opened.st_ino,
                opened.st_mode,
                opened.st_uid,
            ),
        })
        raise checks.CheckExecutionError(
            "stop after owned-parent materialization capture"
        )

    def forbidden_path_materialization(**_kwargs):
        raise AssertionError(
            "checker converted its retained attempt descriptor back to a path"
        )

    monkeypatch.setattr(
        bestplan_promotion,
        "_materialize_integration_tree_at_owned_parent",
        capture_owned_parent,
        raising=False,
    )
    monkeypatch.setattr(
        bestplan_promotion,
        "materialize_integration_tree",
        forbidden_path_materialization,
    )

    with pytest.raises(
        checks.CheckExecutionError,
        match="owned-parent materialization capture",
    ):
        _run_task6_contract(
            checks,
            snapshot=snapshot,
            integration=integration,
            contract=contract,
            commands=required,
            runtime=runtime,
            checks_root=tmp_path / "checks",
            deadline=absolute_deadline,
            cancel_event=cancellation,
        )

    assert len(observed) == 1
    call = observed[0]
    assert "destination" not in call
    assert call["parent_fd"] >= 0
    assert call["parent_identity"] == call["opened_identity"]
    assert call["destination_leaf"] == b"integration"
    assert call["deadline"] == absolute_deadline
    assert call["cancel_event"] is cancellation


def test_owned_attempt_cleanup_closes_descriptor_when_cancelled_at_entry(
    tmp_path, monkeypatch,
):
    checks = _checks()
    checks_root = tmp_path / "checks"
    checks_root.mkdir(mode=0o700)
    prepared_root = checks._prepare_checks_root(
        checks_root,
        forbidden_roots=(),
    )
    attempt = checks._create_owned_attempt(
        prepared_root,
        deadline=time.monotonic() + 30,
    )
    sentinel = attempt.path / "retain.txt"
    sentinel.write_text("retain\n", encoding="utf-8")
    cancellation = threading.Event()
    cancellation.set()
    raw_rmtree_calls = []

    def forbidden_rmtree(*args, **kwargs):
        raw_rmtree_calls.append((args, kwargs))
        raise AssertionError("bounded cleanup delegated to raw rmtree")

    monkeypatch.setattr(checks.shutil, "rmtree", forbidden_rmtree)
    descriptor = attempt.descriptor
    try:
        with pytest.raises(checks.CheckExecutionError, match="cancel"):
            checks._cleanup_owned_attempt(
                attempt,
                deadline=time.monotonic() + 30,
                cancel_event=cancellation,
            )

        with pytest.raises(OSError) as closed:
            os.fstat(descriptor)
        assert closed.value.errno == errno.EBADF
        assert attempt.descriptor == -1
        assert raw_rmtree_calls == []
        assert sentinel.is_file()
    finally:
        attempt.close()
        prepared_root.close()


def test_owned_attempt_cleanup_closes_descriptor_when_deadline_expired_at_entry(
    tmp_path, monkeypatch,
):
    checks = _checks()
    checks_root = tmp_path / "checks"
    checks_root.mkdir(mode=0o700)
    prepared_root = checks._prepare_checks_root(
        checks_root,
        forbidden_roots=(),
    )
    attempt = checks._create_owned_attempt(
        prepared_root,
        deadline=time.monotonic() + 30,
    )
    sentinel = attempt.path / "retain.txt"
    sentinel.write_text("retain\n", encoding="utf-8")
    raw_rmtree_calls = []

    def forbidden_rmtree(*args, **kwargs):
        raw_rmtree_calls.append((args, kwargs))
        raise AssertionError("bounded cleanup delegated to raw rmtree")

    monkeypatch.setattr(checks.shutil, "rmtree", forbidden_rmtree)
    monkeypatch.setattr(checks.time, "monotonic", lambda: 100.0)
    descriptor = attempt.descriptor
    try:
        with pytest.raises(checks.CheckExecutionError, match="deadline"):
            checks._cleanup_owned_attempt(
                attempt,
                deadline=99.0,
            )

        with pytest.raises(OSError) as closed:
            os.fstat(descriptor)
        assert closed.value.errno == errno.EBADF
        assert attempt.descriptor == -1
        assert raw_rmtree_calls == []
        assert sentinel.is_file()
    finally:
        attempt.close()
        prepared_root.close()


@pytest.mark.parametrize("cleanup_kind", ("command-cache", "owned-attempt"))
def test_recursive_check_cleanup_observes_deadline_during_walk_without_rmtree(
    tmp_path, monkeypatch, cleanup_kind,
):
    from agent.bestplan_contract import PinnedInput

    checks = _checks()
    prepared_root = None
    if cleanup_kind == "command-cache":
        integration_root = tmp_path / "integration"
        cache_root = integration_root / ".cache" / "check"
        nested = cache_root / "nested"
        nested.mkdir(parents=True)
        sentinel = nested / "retain.txt"
        sentinel.write_text("retain\n", encoding="utf-8")
        spec = PinnedInput(".cache/check", checks.EMPTY_CACHE_SHA256)

        def cleanup():
            checks._remove_command_cache_roots(
                integration_root,
                (spec,),
                deadline=100.0,
            )
    else:
        checks_root = tmp_path / "checks"
        checks_root.mkdir(mode=0o700)
        prepared_root = checks._prepare_checks_root(
            checks_root,
            forbidden_roots=(),
        )
        attempt = checks._create_owned_attempt(
            prepared_root,
            deadline=time.monotonic() + 30,
        )
        nested = attempt.path / "nested"
        nested.mkdir()
        sentinel = nested / "retain.txt"
        sentinel.write_text("retain\n", encoding="utf-8")

        def cleanup():
            checks._cleanup_owned_attempt(attempt, deadline=100.0)

    clock_calls = 0
    raw_rmtree_calls = []

    def expire_after_entry():
        nonlocal clock_calls
        clock_calls += 1
        return 99.0 if clock_calls == 1 else 101.0

    def forbidden_rmtree(*args, **kwargs):
        raw_rmtree_calls.append((args, kwargs))
        raise AssertionError("bounded cleanup delegated to raw rmtree")

    monkeypatch.setattr(checks.time, "monotonic", expire_after_entry)
    monkeypatch.setattr(checks.shutil, "rmtree", forbidden_rmtree)
    try:
        with pytest.raises(checks.CheckExecutionError, match="deadline"):
            cleanup()
    finally:
        if prepared_root is not None:
            prepared_root.close()

    assert clock_calls >= 2
    assert raw_rmtree_calls == []
    assert sentinel.is_file()


@pytest.mark.parametrize("cleanup_kind", ("command-cache", "owned-attempt"))
def test_recursive_check_cleanup_observes_cancellation_during_walk(
    tmp_path, monkeypatch, cleanup_kind,
):
    from agent.bestplan_contract import PinnedInput

    checks = _checks()

    class CancelDuringCleanup(threading.Event):
        def __init__(self):
            super().__init__()
            self.observations = 0

        def is_set(self):
            self.observations += 1
            return self.observations >= 2

    cancellation = CancelDuringCleanup()
    absolute_deadline = time.monotonic() + 30
    prepared_root = None
    if cleanup_kind == "command-cache":
        cleanup_function = checks._remove_command_cache_roots
        integration_root = tmp_path / "integration"
        cache_root = integration_root / ".cache" / "check"
        nested = cache_root / "nested"
        nested.mkdir(parents=True)
        sentinel = nested / "retain.txt"
        sentinel.write_text("retain\n", encoding="utf-8")
        spec = PinnedInput(".cache/check", checks.EMPTY_CACHE_SHA256)

        def cleanup():
            cleanup_function(
                integration_root,
                (spec,),
                deadline=absolute_deadline,
                cancel_event=cancellation,
            )
    else:
        cleanup_function = checks._cleanup_owned_attempt
        checks_root = tmp_path / "checks"
        checks_root.mkdir(mode=0o700)
        prepared_root = checks._prepare_checks_root(
            checks_root,
            forbidden_roots=(),
        )
        attempt = checks._create_owned_attempt(
            prepared_root,
            deadline=absolute_deadline,
        )
        nested = attempt.path / "nested"
        nested.mkdir()
        sentinel = nested / "retain.txt"
        sentinel.write_text("retain\n", encoding="utf-8")

        def cleanup():
            cleanup_function(
                attempt,
                deadline=absolute_deadline,
                cancel_event=cancellation,
            )

    raw_rmtree_calls = []

    def forbidden_rmtree(*args, **kwargs):
        raw_rmtree_calls.append((args, kwargs))
        raise AssertionError("cancellable cleanup delegated to raw rmtree")

    monkeypatch.setattr(checks.shutil, "rmtree", forbidden_rmtree)
    try:
        assert "cancel_event" in inspect.signature(cleanup_function).parameters
        with pytest.raises(checks.CheckExecutionError, match="cancel"):
            cleanup()
    finally:
        if prepared_root is not None:
            prepared_root.close()

    assert cancellation.observations >= 2
    assert raw_rmtree_calls == []
    assert sentinel.is_file()


@pytest.mark.parametrize("operation", ("cache-scan", "runtime-scan", "teardown"))
def test_support_work_stops_at_an_expired_absolute_deadline(
    tmp_path, monkeypatch, operation,
):
    from agent.bestplan_contract import PinnedInput

    checks = _checks()
    lower_calls = []

    def forbidden_lower_work(*args, **kwargs):
        lower_calls.append((args, kwargs))
        raise AssertionError("support work started after its absolute deadline")

    if operation == "cache-scan":
        seed_root = tmp_path / "seed"
        source = seed_root / ".cache" / "check"
        source.mkdir(parents=True)
        (source / "payload.bin").write_bytes(b"cache")
        spec = PinnedInput(
            ".cache/check",
            checks.pinned_path_sha256(source),
        )
        target_root = tmp_path / "integration"
        target_root.mkdir()
        monkeypatch.setattr(checks, "_capture_check_tree", forbidden_lower_work)

        def cache_call():
            return checks._copy_cache_seed(
                seed_root,
                target_root,
                spec,
                deadline=time.monotonic() - 1,
            )
        operation_call = cache_call
    elif operation == "runtime-scan":
        _snapshot, _contract, controller, _required = _task6_contract_bundle(tmp_path)
        runtime = _task6_runtime(checks, tmp_path, controller)
        monkeypatch.setattr(
            checks, "_require_pinned_regular_file", forbidden_lower_work,
        )
        monkeypatch.setattr(
            checks,
            "candidate_controller_artifact_sha256",
            forbidden_lower_work,
        )
        monkeypatch.setattr(checks, "pinned_path_sha256", forbidden_lower_work)

        def runtime_call():
            return checks._assert_host_runtime(
                runtime,
                deadline=time.monotonic() - 1,
            )
        operation_call = runtime_call
    else:
        attempt = tmp_path / "bestplan-check-expired"
        attempt.mkdir()
        monkeypatch.setattr(checks.shutil, "rmtree", forbidden_lower_work)

        def cleanup_call():
            return checks._cleanup_owned_attempt(
                attempt,
                deadline=time.monotonic() - 1,
            )
        operation_call = cleanup_call

    started = time.monotonic()
    with pytest.raises(checks.CheckError, match="deadline|timeout|cleanup"):
        operation_call()
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert lower_calls == []
