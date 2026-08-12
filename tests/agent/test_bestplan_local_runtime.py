from __future__ import annotations

import copy
import hashlib
import math
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent.bestplan_contract import BoundCommand
from agent.bestplan_source import SourceSnapshot


def _check_config() -> dict[str, list[str]]:
    return {"pytest_nodes": ["tests/test_sample.py::test_sample"]}


def _manifest() -> dict[str, object]:
    return {
        "version": 1,
        "mode": "sota",
        "slices": [
            {
                "id": "slice-1",
                "kind": "implementation",
                "goal": "Make the approved change",
                "read_only": False,
                "acceptance": [
                    "pytest -q -- tests/test_sample.py::test_sample",
                ],
            },
        ],
    }


def _contract(manifest_digest: str) -> dict[str, object]:
    return {
        "schema": "hermes.bestplan.local-go.v1",
        "version": 1,
        "mode": "local_main",
        "repository": {
            "repository_id": "repo-local-main",
            "workspace": "/tmp/repo",
            "workspace_raw_b64": "L3RtcC9yZXBv",
            "worktree": "/tmp/repo",
            "worktree_raw_b64": "L3RtcC9yZXBv",
            "git_dir": "/tmp/repo/.git",
            "git_dir_raw_b64": "L3RtcC9yZXBvLy5naXQ=",
            "common_dir": "/tmp/repo/.git",
            "common_dir_raw_b64": "L3RtcC9yZXBvLy5naXQ=",
            "common_dir_device": 11,
            "common_dir_inode": 22,
            "object_format": "sha1",
        },
        "source": {
            "base_oid": "1" * 40,
            "tree_oid": "2" * 40,
            "local_ref": "refs/heads/main",
            "snapshot_digest": "3" * 64,
            "source_digest": "4" * 64,
            "protected_digest": "5" * 64,
        },
        "manifest_digest": manifest_digest,
        "check_runtime_digest": "6" * 64,
        "commands": [
            {
                "identifier": "pytest",
                "executable": "/usr/bin/python3",
                "executable_sha256": "7" * 64,
                "argv": [
                    "-I", "-B", "-m", "pytest", "-q", "--",
                    "tests/test_sample.py::test_sample",
                ],
                "logical_cwd": "integration",
                "env": [{"name": "PYTHONHASHSEED", "value": "0"}],
                "inputs": [],
                "cache": [],
                "timeout_seconds": 600,
                "network_allowlist": [],
            },
        ],
        "controller": {
            "repository_id": "repo-local-main",
            "controller_id": "local-controller",
            "release_oid": "8" * 40,
            "artifact_sha256": "9" * 64,
        },
    }


def _git_repository(
    root: Path,
    files: dict[str, str],
) -> tuple[Path, str]:
    root.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=root, check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "tests@example.invalid"],
        cwd=root, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "BestPlan Tests"],
        cwd=root, check=True,
    )
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-m", "base"], cwd=root, check=True,
        capture_output=True,
    )
    release_oid = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    return root, release_oid


def _target_snapshot(tmp_path: Path) -> SourceSnapshot:
    from agent.bestplan_source import capture_source_snapshot, resolve_repo_identity

    root, _release = _git_repository(
        tmp_path / "target",
        {
            "pyproject.toml": "[tool.pytest.ini_options]\n",
            "test_sample.py": "def test_sample():\n    assert True\n",
        },
    )
    repo = resolve_repo_identity(root)
    return capture_source_snapshot(repo, time.monotonic() + 10.0)


def _controller_checkout(
    tmp_path: Path,
    *,
    extra_files: dict[str, str] | None = None,
) -> tuple[Path, str]:
    files = {
        "agent/bestplan_worker.py": "# retained worker\n",
        "agent/controller_helper.py": "VALUE = 1\n",
        ".env.example": "API_KEY=replace-me\n",
    }
    files.update(extra_files or {})
    return _git_repository(tmp_path / "controller", files)


def _fake_check_plan(tmp_path: Path):
    from agent.bestplan_checks import (
        CHECK_SANDBOX_POLICY_VERSION,
        PinnedRuntimePath,
    )
    from agent.bestplan_local import LocalCheckPlan

    executable = tmp_path / "python-runtime" / "bin" / "python3.11"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"python-runtime\n")
    executable.chmod(0o755)
    launcher = tmp_path / "python-venv" / "bin" / "python"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(executable)
    stdlib = tmp_path / "python-runtime" / "lib" / "python3.11"
    (stdlib / "lib-dynload").mkdir(parents=True)
    site = tmp_path / "python-venv" / "lib" / "python3.11" / "site-packages"
    pytest_module = site / "pytest" / "__init__.py"
    pytest_module.parent.mkdir(parents=True)
    pytest_module.write_text("__version__ = 'test'\n", encoding="utf-8")
    executable_digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    command = BoundCommand(
        identifier="pytest",
        executable=str(executable),
        executable_sha256=executable_digest,
        argv=(
            "-I", "-B", "-m", "pytest", "-q", "--",
            "tests/test_sample.py::test_sample",
        ),
        logical_cwd="integration",
        env=(
            ("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1"),
            ("PYTHONHASHSEED", "0"),
            ("__PYVENV_LAUNCHER__", str(launcher)),
        ),
        inputs=(),
        cache=(),
        timeout_seconds=600,
        network_allowlist=(),
    )
    plan = LocalCheckPlan(
        commands=(command,),
        runtime_read_paths=(
            PinnedRuntimePath(path=site, sha256="a" * 64),
        ),
        sandbox_executable=Path("/usr/bin/sandbox-exec"),
        sandbox_executable_sha256="b" * 64,
        policy_version=CHECK_SANDBOX_POLICY_VERSION,
        check_runtime_digest="c" * 64,
        pytest_module_path=pytest_module,
    )
    return launcher, plan


def _stub_check_capture(monkeypatch, module, plan):
    calls: list[dict[str, object]] = []

    def derive(**kwargs):
        calls.append(dict(kwargs))
        return plan

    monkeypatch.setattr(module, "derive_local_check_plan", derive)
    return calls


def _local_contract_for_inputs(module, snapshot, manifest, inputs):
    return module.build_local_go_contract(
        snapshot=snapshot,
        controller=inputs.controller,
        commands=inputs.check_plan.commands,
        manifest_digest=module.local_go_manifest_digest(manifest),
        check_runtime_digest=inputs.check_plan.check_runtime_digest,
    )


def test_local_go_manifest_digest_uses_strict_canonical_manifest_bytes():
    from agent.bestplan_contract import canonical_json
    from agent.bestplan_local import local_go_manifest_digest

    manifest = _manifest()
    expected = hashlib.sha256(
        canonical_json(manifest).encode("utf-8"),
    ).hexdigest()

    assert local_go_manifest_digest(manifest) == expected
    assert local_go_manifest_digest(dict(reversed(tuple(manifest.items())))) == expected
    assert local_go_manifest_digest({**manifest, "mode": "fast"}) != expected


def test_local_go_approval_digest_binds_manifest_and_exact_local_contract():
    from agent.bestplan_contract import canonical_json
    from agent.bestplan_local import (
        LocalGoValidationError,
        local_go_approval_digest,
        local_go_contract_json,
        local_go_manifest_digest,
    )

    manifest = _manifest()
    contract = _contract(local_go_manifest_digest(manifest))
    expected = hashlib.sha256(
        b"hermes.bestplan.local-go-approval.v1\0"
        + canonical_json(manifest).encode("utf-8")
        + b"\0"
        + local_go_contract_json(contract).encode("utf-8"),
    ).hexdigest()

    assert local_go_approval_digest(manifest, contract) == expected
    changed = {**contract, "check_runtime_digest": "a" * 64}
    assert local_go_approval_digest(manifest, changed) != expected
    with pytest.raises(LocalGoValidationError, match="manifest digest"):
        local_go_approval_digest({**manifest, "mode": "fast"}, contract)


def test_capture_local_execution_inputs_retains_exact_read_only_controller(
    monkeypatch, tmp_path,
):
    import agent.bestplan_local as bestplan_local
    import hermes_constants
    from agent.bestplan_sandbox import candidate_controller_artifact_sha256

    snapshot = _target_snapshot(tmp_path)
    controller_checkout, release_oid = _controller_checkout(tmp_path)
    launcher, check_plan = _fake_check_plan(tmp_path)
    calls = _stub_check_capture(monkeypatch, bestplan_local, check_plan)
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir(mode=0o700)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: hermes_home)
    deadline = time.monotonic() + 10.0
    cancellation = threading.Event()

    captured = bestplan_local.capture_local_execution_inputs(
        snapshot=snapshot,
        controller_python=launcher,
        manifest=_manifest(),
        deadline=deadline,
        cancel_event=cancellation,
        _controller_checkout=controller_checkout,
    )

    assert isinstance(captured, bestplan_local.LocalExecutionInputs)
    assert captured.check_plan is check_plan
    assert captured.controller.repository_id == snapshot.repo.repository_id
    assert captured.controller.release_oid == release_oid
    assert captured.controller.artifact_sha256 == (
        candidate_controller_artifact_sha256(captured.controller_source)
    )
    assert captured.controller_source.is_relative_to(hermes_home)
    assert not captured.controller_source.is_relative_to(
        Path(snapshot.repo.worktree),
    )
    assert (captured.controller_source / "agent" / "bestplan_worker.py").is_file()
    assert not (captured.controller_source / ".git").exists()
    assert not (captured.controller_source / ".env").exists()
    for root, directories, files in os.walk(captured.controller_source):
        for name in [".", *directories, *files]:
            path = Path(root) if name == "." else Path(root) / name
            assert stat.S_IMODE(path.lstat().st_mode) & 0o222 == 0
    assert calls == [
        {
            "snapshot": snapshot,
            "controller_python": launcher,
            "config": _check_config(),
            "deadline": deadline,
            "cancel_event": cancellation,
        },
    ]

    reused = bestplan_local.capture_local_execution_inputs(
        snapshot=snapshot,
        controller_python=launcher,
        manifest=_manifest(),
        deadline=time.monotonic() + 10.0,
        cancel_event=cancellation,
        _controller_checkout=controller_checkout,
    )
    assert reused.controller_source == captured.controller_source
    assert reused.controller == captured.controller


def test_capture_rejects_tampered_retained_controller_without_replacing_it(
    monkeypatch, tmp_path,
):
    import agent.bestplan_local as bestplan_local
    import hermes_constants

    snapshot = _target_snapshot(tmp_path)
    controller_checkout, _release_oid = _controller_checkout(tmp_path)
    launcher, check_plan = _fake_check_plan(tmp_path)
    calls = _stub_check_capture(monkeypatch, bestplan_local, check_plan)
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir(mode=0o700)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: hermes_home)
    captured = bestplan_local.capture_local_execution_inputs(
        snapshot=snapshot,
        controller_python=launcher,
        manifest=_manifest(),
        deadline=time.monotonic() + 10.0,
        _controller_checkout=controller_checkout,
    )
    worker = captured.controller_source / "agent" / "bestplan_worker.py"
    worker.chmod(0o600)
    worker.write_text("# substituted controller\n", encoding="utf-8")

    with pytest.raises(
        bestplan_local.LocalGoValidationError, match="controller.*changed|artifact",
    ):
        bestplan_local.capture_local_execution_inputs(
            snapshot=snapshot,
            controller_python=launcher,
            manifest=_manifest(),
            deadline=time.monotonic() + 10.0,
            _controller_checkout=controller_checkout,
        )

    assert worker.read_text(encoding="utf-8") == "# substituted controller\n"
    assert len(calls) == 1


def test_capture_rejects_non_git_or_secret_bearing_controller_before_check_plan(
    monkeypatch, tmp_path,
):
    import agent.bestplan_local as bestplan_local
    import hermes_constants

    snapshot = _target_snapshot(tmp_path)
    launcher, check_plan = _fake_check_plan(tmp_path)
    calls = _stub_check_capture(monkeypatch, bestplan_local, check_plan)
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir(mode=0o700)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: hermes_home)
    non_git = tmp_path / "not-a-git-controller"
    non_git.mkdir()

    with pytest.raises(bestplan_local.LocalGoValidationError, match="Git checkout"):
        bestplan_local.capture_local_execution_inputs(
            snapshot=snapshot,
            controller_python=launcher,
            manifest=_manifest(),
            deadline=time.monotonic() + 10.0,
            _controller_checkout=non_git,
        )

    secret_checkout, _release = _controller_checkout(
        tmp_path / "secret-case",
        extra_files={".env": "API_KEY=real-secret\n"},
    )
    with pytest.raises(
        bestplan_local.LocalGoValidationError, match="secret|environment",
    ):
        bestplan_local.capture_local_execution_inputs(
            snapshot=snapshot,
            controller_python=launcher,
            manifest=_manifest(),
            deadline=time.monotonic() + 10.0,
            _controller_checkout=secret_checkout,
        )

    assert calls == []


def test_build_local_execution_runtime_matches_contract_and_creates_private_roots(
    monkeypatch, tmp_path,
):
    import agent.bestplan_local as bestplan_local
    import hermes_constants
    from agent.bestplan_checks import CheckHostRuntime
    from tools.delegate_tool import BestplanHostRuntime

    snapshot = _target_snapshot(tmp_path)
    controller_checkout, _release_oid = _controller_checkout(tmp_path)
    launcher, check_plan = _fake_check_plan(tmp_path)
    calls = _stub_check_capture(monkeypatch, bestplan_local, check_plan)
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir(mode=0o700)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: hermes_home)
    manifest = _manifest()
    captured = bestplan_local.capture_local_execution_inputs(
        snapshot=snapshot,
        controller_python=launcher,
        manifest=_manifest(),
        deadline=time.monotonic() + 10.0,
        _controller_checkout=controller_checkout,
    )
    contract = _local_contract_for_inputs(
        bestplan_local, snapshot, manifest, captured,
    )

    runtime = bestplan_local.build_local_execution_runtime(
        plan_id="plan-runtime-1",
        snapshot=snapshot,
        manifest=manifest,
        contract=contract,
        controller_python=launcher,
        deadline=time.monotonic() + 10.0,
        _controller_checkout=controller_checkout,
    )

    assert isinstance(runtime, bestplan_local.LocalExecutionRuntime)
    assert isinstance(runtime.candidate_runtime, BestplanHostRuntime)
    assert isinstance(runtime.check_runtime, CheckHostRuntime)
    assert runtime.check_plan is check_plan
    assert runtime.candidate_runtime.controller == captured.controller
    assert runtime.check_runtime.controller == captured.controller
    assert runtime.candidate_runtime.controller_source == captured.controller_source
    assert runtime.check_runtime.controller_source == captured.controller_source
    assert runtime.candidate_runtime.controller_python == launcher.absolute()
    assert runtime.check_runtime.controller_python_launcher == launcher.absolute()
    assert runtime.check_runtime.pytest_module_path == check_plan.pytest_module_path
    assert runtime.check_runtime.runtime_read_paths == check_plan.runtime_read_paths
    assert runtime.check_runtime.sandbox_executable == check_plan.sandbox_executable
    assert runtime.check_runtime.sandbox_executable_sha256 == (
        check_plan.sandbox_executable_sha256
    )
    assert runtime.operation_timeout_seconds == 3600.0
    assert math.isfinite(runtime.operation_timeout_seconds)

    roots = {
        runtime.candidate_runtime.attempts_root,
        runtime.integration_root,
        runtime.checks_root,
        runtime.check_runtime.cache_seed_root,
    }
    assert len(roots) == 4
    for root in roots:
        assert root.is_relative_to(hermes_home)
        info = root.lstat()
        assert stat.S_ISDIR(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o700
        assert info.st_uid == os.geteuid()
        assert not root.is_relative_to(Path(snapshot.repo.worktree))
        assert not root.is_relative_to(captured.controller_source)
    assert len(calls) == 2
    assert [call["config"] for call in calls] == [
        _check_config(),
        _check_config(),
    ]


def test_build_rejects_any_stored_relation_drift_before_creating_plan_roots(
    monkeypatch, tmp_path,
):
    import agent.bestplan_local as bestplan_local
    import hermes_constants

    snapshot = _target_snapshot(tmp_path)
    controller_checkout, _release_oid = _controller_checkout(tmp_path)
    launcher, check_plan = _fake_check_plan(tmp_path)
    calls = _stub_check_capture(monkeypatch, bestplan_local, check_plan)
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir(mode=0o700)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: hermes_home)
    manifest = _manifest()
    captured = bestplan_local.capture_local_execution_inputs(
        snapshot=snapshot,
        controller_python=launcher,
        manifest=_manifest(),
        deadline=time.monotonic() + 10.0,
        _controller_checkout=controller_checkout,
    )
    contract = _local_contract_for_inputs(
        bestplan_local, snapshot, manifest, captured,
    )

    manifest_changed = {**manifest, "mode": "fast"}
    command_changed = copy.deepcopy(contract)
    command_changed["commands"][0]["argv"] = ["-m", "compileall", "."]
    acceptance_changed = copy.deepcopy(contract)
    acceptance_changed["commands"][0]["argv"][-1] = (
        "tests/test_other.py::test_other"
    )
    runtime_changed = {**contract, "check_runtime_digest": "d" * 64}
    controller_changed = copy.deepcopy(contract)
    controller_changed["controller"]["artifact_sha256"] = "e" * 64
    source_changed = copy.deepcopy(contract)
    source_changed["source"]["base_oid"] = "f" * 40
    repository_changed = copy.deepcopy(contract)
    repository_changed["repository"]["common_dir_inode"] += 1
    cases = (
        (manifest_changed, contract),
        (manifest, command_changed),
        (manifest, acceptance_changed),
        (manifest, runtime_changed),
        (manifest, controller_changed),
        (manifest, source_changed),
        (manifest, repository_changed),
    )

    for index, (case_manifest, case_contract) in enumerate(cases):
        call_count = len(calls)
        with pytest.raises(bestplan_local.LocalGoValidationError):
            bestplan_local.build_local_execution_runtime(
                plan_id=f"drift-{index}",
                snapshot=snapshot,
                manifest=case_manifest,
                contract=case_contract,
                controller_python=launcher,
                deadline=time.monotonic() + 10.0,
                _controller_checkout=controller_checkout,
            )
        if case_contract is acceptance_changed:
            assert len(calls) == call_count
        assert not (
            hermes_home / "bestplan-local-go" / "plans"
        ).exists()


def test_build_runtime_rejects_invalid_or_cancelled_control_before_plan_roots(
    monkeypatch, tmp_path,
):
    import agent.bestplan_local as bestplan_local
    import hermes_constants

    snapshot = _target_snapshot(tmp_path)
    controller_checkout, _release_oid = _controller_checkout(tmp_path)
    launcher, check_plan = _fake_check_plan(tmp_path)
    _stub_check_capture(monkeypatch, bestplan_local, check_plan)
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir(mode=0o700)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: hermes_home)
    manifest = _manifest()
    captured = bestplan_local.capture_local_execution_inputs(
        snapshot=snapshot,
        controller_python=launcher,
        manifest=_manifest(),
        deadline=time.monotonic() + 10.0,
        _controller_checkout=controller_checkout,
    )
    contract = _local_contract_for_inputs(
        bestplan_local, snapshot, manifest, captured,
    )
    cancelled = threading.Event()
    cancelled.set()

    for deadline, cancel_event in (
        (math.inf, None),
        (time.monotonic() - 1.0, None),
        (time.monotonic() + 10.0, cancelled),
    ):
        with pytest.raises(bestplan_local.LocalGoValidationError):
            bestplan_local.build_local_execution_runtime(
                plan_id="control-rejected",
                snapshot=snapshot,
                manifest=manifest,
                contract=contract,
                controller_python=launcher,
                deadline=deadline,
                cancel_event=cancel_event,
                _controller_checkout=controller_checkout,
            )
    assert not (hermes_home / "bestplan-local-go" / "plans").exists()


def test_build_reuses_only_exact_empty_private_plan_roots(
    monkeypatch, tmp_path,
):
    import agent.bestplan_local as bestplan_local
    import hermes_constants

    snapshot = _target_snapshot(tmp_path)
    controller_checkout, _release_oid = _controller_checkout(tmp_path)
    launcher, check_plan = _fake_check_plan(tmp_path)
    _stub_check_capture(monkeypatch, bestplan_local, check_plan)
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir(mode=0o700)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: hermes_home)
    manifest = _manifest()
    captured = bestplan_local.capture_local_execution_inputs(
        snapshot=snapshot,
        controller_python=launcher,
        manifest=_manifest(),
        deadline=time.monotonic() + 10.0,
        _controller_checkout=controller_checkout,
    )
    contract = _local_contract_for_inputs(
        bestplan_local, snapshot, manifest, captured,
    )

    def build(plan_id: str):
        return bestplan_local.build_local_execution_runtime(
            plan_id=plan_id,
            snapshot=snapshot,
            manifest=manifest,
            contract=contract,
            controller_python=launcher,
            deadline=time.monotonic() + 10.0,
            _controller_checkout=controller_checkout,
        )

    initial = build("retry-clean")
    retry = build("retry-clean")
    assert retry == initial

    dirty = build("retry-dirty")
    (dirty.candidate_runtime.attempts_root / "candidate.json").write_text(
        "untrusted stale output\n", encoding="utf-8",
    )
    with pytest.raises(bestplan_local.LocalGoValidationError, match="not empty"):
        build("retry-dirty")

    wrong_mode = build("retry-mode")
    wrong_mode.checks_root.chmod(0o755)
    with pytest.raises(bestplan_local.LocalGoValidationError, match="unsafe"):
        build("retry-mode")

    replaced = build("retry-identity")
    displaced = replaced.integration_root.parent / "displaced-integration"
    replaced.integration_root.rename(displaced)
    outside = tmp_path / "outside-replacement"
    outside.mkdir(mode=0o700)
    replaced.integration_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(bestplan_local.LocalGoValidationError, match="unsafe"):
        build("retry-identity")
    assert list(outside.iterdir()) == []
