from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

from agent.bestplan_contract import (
    BoundCommand,
    ControllerIdentity,
    EnrolledRepository,
)
from agent.bestplan_source import (
    IndexEntry,
    IndexFlags,
    ProtectedManifest,
    ProtectedPath,
    RepoIdentity,
    SourceSnapshot,
)


def _repo(tmp_path: Path) -> RepoIdentity:
    worktree = tmp_path / "repo"
    git_dir = worktree / ".git"
    worktree.mkdir()
    git_dir.mkdir()
    return RepoIdentity(
        workspace=str(worktree),
        workspace_raw=str(worktree).encode(),
        worktree=str(worktree),
        worktree_raw=str(worktree).encode(),
        git_dir=str(git_dir),
        git_dir_raw=str(git_dir).encode(),
        common_dir=str(git_dir),
        common_dir_raw=str(git_dir).encode(),
        common_dir_device=11,
        common_dir_inode=22,
        object_format="sha1",
        repository_id="repo-local-main",
    )


def _snapshot(tmp_path: Path) -> SourceSnapshot:
    repo = _repo(tmp_path)
    protected = ProtectedManifest(
        index_entries=(IndexEntry(b"tracked.py", 0o100644, "1" * 40, 0),),
        index_flags=(
            IndexFlags(b"tracked.py", b"H ", b"", False, False, False, False),
        ),
        worktree_entries=(
            ProtectedPath(
                path=b"notes.txt",
                tracked=False,
                kind="regular",
                mode=0o100644,
                size=5,
                content_sha256="2" * 64,
                symlink_target=None,
                git_oid=None,
            ),
        ),
        protected_paths=(b"notes.txt",),
        staged_diff_sha256="3" * 64,
        unstaged_diff_sha256="4" * 64,
        digest="5" * 64,
    )
    return SourceSnapshot(
        repo=repo,
        head_symbolic=True,
        head_ref=b"refs/heads/main",
        head_raw=b"ref: refs/heads/main\n",
        head_oid="6" * 40,
        tree_oid="7" * 40,
        protected_manifest=protected,
        capture_implementation_sha256="8" * 64,
        fingerprint="9" * 64,
    )


def _controller(repo: RepoIdentity) -> ControllerIdentity:
    return ControllerIdentity(
        repository_id=repo.repository_id,
        controller_id="local-controller",
        release_oid="a" * 40,
        artifact_sha256="b" * 64,
    )


def _command() -> BoundCommand:
    return BoundCommand(
        identifier="pytest",
        executable=sys.executable,
        executable_sha256=hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
        argv=(
            "-I", "-B", "-m", "pytest", "-q", "--",
            "tests/test_sample.py::test_sample",
        ),
        logical_cwd="integration",
        env=(("PYTHONHASHSEED", "0"),),
        inputs=(),
        cache=(),
        timeout_seconds=600,
        network_allowlist=(),
    )


def _captured_python_repo(tmp_path: Path, *, pytest_project: bool) -> SourceSnapshot:
    workspace = tmp_path / "project"
    workspace.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=workspace, check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "tests@example.invalid"],
        cwd=workspace, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "BestPlan Tests"],
        cwd=workspace, check=True,
    )
    if pytest_project:
        (workspace / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\naddopts = '-ra'\n",
            encoding="utf-8",
        )
        (workspace / "tests").mkdir()
        (workspace / "tests" / "test_sample.py").write_text(
            "def test_sample():\n    assert True\n\n"
            "def test_second():\n    assert True\n",
            encoding="utf-8",
        )
    else:
        (workspace / "package.json").write_text(
            '{"scripts":{"test":"vitest"}}',
            encoding="utf-8",
        )
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(
        ["git", "commit", "-m", "base"], cwd=workspace, check=True,
        capture_output=True,
    )
    from agent.bestplan_source import capture_source_snapshot, resolve_repo_identity

    repo = resolve_repo_identity(str(workspace))
    return capture_source_snapshot(repo, time.monotonic() + 20.0)


def test_local_go_contract_contains_only_local_execution_authority(tmp_path):
    from agent.bestplan_local import (
        LOCAL_GO_CONTRACT_SCHEMA,
        build_local_go_contract,
        local_go_contract_digest,
        validate_local_go_contract,
    )

    snapshot = _snapshot(tmp_path)
    contract = build_local_go_contract(
        snapshot=snapshot,
        controller=_controller(snapshot.repo),
        commands=(_command(),),
        manifest_digest="c" * 64,
        check_runtime_digest="d" * 64,
    )

    assert set(contract) == {
        "schema",
        "version",
        "mode",
        "repository",
        "source",
        "manifest_digest",
        "check_runtime_digest",
        "commands",
        "controller",
    }
    assert contract["schema"] == LOCAL_GO_CONTRACT_SCHEMA
    assert contract["version"] == 1
    assert contract["mode"] == "local_main"
    assert contract["manifest_digest"] == "c" * 64
    assert contract["check_runtime_digest"] == "d" * 64
    assert "publication" not in contract
    assert "review" not in contract
    assert "live_target" not in contract
    assert validate_local_go_contract(contract) == contract
    assert len(local_go_contract_digest(contract)) == 64


def test_local_go_contract_binds_source_repository_controller_and_commands(tmp_path):
    from agent.bestplan_local import (
        LocalGoValidationError,
        build_local_go_contract,
        local_go_contract_digest,
        validate_local_go_contract,
    )

    snapshot = _snapshot(tmp_path)
    contract = build_local_go_contract(
        snapshot=snapshot,
        controller=_controller(snapshot.repo),
        commands=(_command(),),
        manifest_digest="c" * 64,
        check_runtime_digest="d" * 64,
    )
    baseline = local_go_contract_digest(contract)

    for key, replacement in (
        ("manifest_digest", "d" * 64),
        ("mode", "remote_publish"),
    ):
        changed = dict(contract)
        changed[key] = replacement
        if key == "mode":
            with pytest.raises(LocalGoValidationError):
                validate_local_go_contract(changed)
        else:
            assert local_go_contract_digest(changed) != baseline

    changed_source = {**contract, "source": {**contract["source"], "base_oid": "e" * 40}}
    assert local_go_contract_digest(changed_source) != baseline
    changed_controller = {
        **contract,
        "controller": {**contract["controller"], "artifact_sha256": "f" * 64},
    }
    assert local_go_contract_digest(changed_controller) != baseline
    changed_command = {
        **contract,
        "commands": [
            {**contract["commands"][0], "argv": ["-m", "compileall", "."]},
        ],
    }
    assert local_go_contract_digest(changed_command) != baseline
    changed_runtime = {**contract, "check_runtime_digest": "0" * 64}
    assert local_go_contract_digest(changed_runtime) != baseline


def test_local_go_render_states_exact_go_and_push_boundary(tmp_path):
    from agent.bestplan_local import build_local_go_contract, render_local_go_contract

    snapshot = _snapshot(tmp_path)
    contract = build_local_go_contract(
        snapshot=snapshot,
        controller=_controller(snapshot.repo),
        commands=(_command(),),
        manifest_digest="c" * 64,
        check_runtime_digest="d" * 64,
    )

    rendered = render_local_go_contract(contract)
    assert "fast-forward" in rendered
    assert "local `main`" in rendered
    assert "does not authorize a remote push" in rendered
    assert "pytest" in rendered
    assert "tests/test_sample.py::test_sample" in rendered
    assert "approved checks" in rendered
    assert "live" not in rendered.casefold()
    assert "rollback" not in rendered.casefold()


def test_protocol2_runtime_identity_binds_endpoint_api_mode_and_request_overrides():
    from tools.delegate_tool import _bestplan_runtime_identity

    task = {"route": "code_worker", "_bestplan_read_only": False}
    base = {
        "route": "code_worker",
        "provider": "custom",
        "model": "worker-model",
        "base_url": "HTTPS://EXAMPLE.test:443/v1/",
        "api_mode": "chat_completions",
        "request_overrides": {"temperature": 0.1},
    }
    first = _bestplan_runtime_identity(task, base, execution_protocol=2)
    endpoint_changed = _bestplan_runtime_identity(
        task,
        {**base, "base_url": "https://other.example/v1"},
        execution_protocol=2,
    )
    mode_changed = _bestplan_runtime_identity(
        task,
        {**base, "api_mode": "codex_responses"},
        execution_protocol=2,
    )
    overrides_changed = _bestplan_runtime_identity(
        task,
        {**base, "request_overrides": {"temperature": 0.2}},
        execution_protocol=2,
    )

    identity = first["runtime_identity"]
    assert identity["endpoint"] == "https://example.test/v1"
    assert identity["api_mode"] == "chat_completions"
    assert identity["request_overrides"] == {"temperature": 0.1}
    assert len(
        {
            first["runtime_fingerprint"],
            endpoint_changed["runtime_fingerprint"],
            mode_changed["runtime_fingerprint"],
            overrides_changed["runtime_fingerprint"],
        }
    ) == 4


@pytest.mark.parametrize(
    "endpoint",
    (
        "https://user@example.test/v1",
        "https://user:password@example.test/v1",
        "https://example.test/v1?api_key=secret",
        "https://example.test/v1#different-route",
        "https://example.test/v1?",
        "https://example.test/v1#",
    ),
)
def test_protocol2_endpoint_identity_rejects_unbound_or_credential_parts(endpoint):
    from tools.delegate_tool import _endpoint_identity

    with pytest.raises(ValueError, match="endpoint"):
        _endpoint_identity(endpoint)


def test_protocol2_direct_endpoint_requires_key_or_explicit_no_auth(monkeypatch):
    import tools.delegate_tool as delegate_tool

    task = {"route": "code_worker", "_bestplan_read_only": False}
    lane = {
        "provider": "custom",
        "model": "worker-model",
        "base_url": "https://example.test/v1",
    }
    monkeypatch.setattr(
        delegate_tool,
        "_load_config",
        lambda: {"lanes": {"code_worker": lane}},
    )
    resolved = {
        "provider": "custom",
        "model": "worker-model",
        "base_url": "https://example.test/v1",
        "api_mode": "chat_completions",
        "api_key": None,
    }
    monkeypatch.setattr(
        delegate_tool,
        "_resolve_delegation_credentials",
        lambda _lane, _parent: dict(resolved),
    )

    with pytest.raises(ValueError, match="api_key|no_auth"):
        delegate_tool.resolve_bestplan_runtime_specs(
            [task], SimpleNamespace(api_key="must-not-fall-back"),
            execution_protocol=2,
        )

    lane["no_auth"] = True
    [runtime] = delegate_tool.resolve_bestplan_runtime_specs(
        [task], SimpleNamespace(api_key="must-not-fall-back"),
        execution_protocol=2,
    )
    assert runtime["no_auth"] is True
    assert runtime["runtime_identity"]["auth_mode"] == "none"
    assert "must-not-fall-back" not in json.dumps(runtime["runtime_identity"])

    resolved["provider"] = "openai-codex"
    resolved["api_mode"] = "codex_responses"
    with pytest.raises(ValueError, match="no_auth"):
        delegate_tool.resolve_bestplan_runtime_specs(
            [task], SimpleNamespace(api_key="must-not-fall-back"),
            execution_protocol=2,
        )


def test_protocol2_runtime_identity_rejects_secret_request_overrides():
    from tools.delegate_tool import _bestplan_runtime_identity

    task = {"route": "code_worker", "_bestplan_read_only": False}
    runtime = {
        "route": "code_worker",
        "provider": "custom",
        "model": "worker-model",
        "base_url": "https://example.test/v1",
        "api_mode": "chat_completions",
        "api_key": "parent-secret",
        "no_auth": False,
        "request_overrides": {
            "extra_headers": {"Authorization": "Bearer parent-secret"},
        },
    }

    with pytest.raises(ValueError, match="override"):
        _bestplan_runtime_identity(task, runtime, execution_protocol=2)


def test_local_runtime_deadline_is_finite_and_clamped(monkeypatch):
    import agent.bestplan_local as bestplan_local

    monkeypatch.setattr(bestplan_local.time, "monotonic", lambda: 100.0)
    assert bestplan_local._bounded_local_capture_deadline(None) == 160.0
    assert bestplan_local._bounded_local_capture_deadline(1000.0) == 160.0
    assert bestplan_local._bounded_local_capture_deadline(150.0) == 150.0
    assert bestplan_local._bounded_local_capture_deadline(110.0) == 110.0
    for invalid in (True, "110", math.nan, math.inf, -math.inf, 100.0):
        with pytest.raises(bestplan_local.LocalGoValidationError, match="deadline"):
            bestplan_local._bounded_local_capture_deadline(invalid)


def test_local_check_identity_capture_preserves_admitted_deadline(
    monkeypatch, tmp_path,
):
    import agent.bestplan_local as bestplan_local
    import agent.bestplan_sandbox as bestplan_sandbox

    snapshot = _captured_python_repo(tmp_path, pytest_project=True)
    launcher = tmp_path / "venv" / "bin" / "python"
    executable = tmp_path / "runtime" / "python"
    pytest_module = tmp_path / "runtime" / "pytest" / "__init__.py"
    observed_deadlines: list[float] = []

    monkeypatch.setattr(bestplan_local.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(
        bestplan_local,
        "_captured_pytest_marker",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bestplan_local,
        "_local_python_launch",
        lambda _path: (launcher, executable, None),
    )

    def launcher_identity(_launcher, _executable, budget):
        observed_deadlines.append(budget.deadline)
        return {"resolved_identity": {"sha256": "1" * 64}}

    def runtime_paths(
        _launcher, _pyvenv, *, budget, deadline, cancel_event,
    ):
        observed_deadlines.extend((budget.deadline, deadline))
        return (), ()

    def artifact_identity(_path, budget):
        observed_deadlines.append(budget.deadline)
        return {"sha256": "2" * 64}

    monkeypatch.setattr(
        bestplan_sandbox, "_launcher_identity", launcher_identity,
    )
    monkeypatch.setattr(
        bestplan_local, "_capture_runtime_read_paths", runtime_paths,
    )
    monkeypatch.setattr(
        bestplan_sandbox, "_stable_artifact_tree_identity", artifact_identity,
    )
    monkeypatch.setattr(
        bestplan_local,
        "_probe_local_pytest_import",
        lambda **_kwargs: pytest_module,
    )

    bestplan_local.derive_local_check_plan(
        snapshot=snapshot,
        controller_python=launcher,
        config={"pytest_nodes": ["tests/test_sample.py::test_sample"]},
        deadline=150.0,
    )

    assert observed_deadlines == [150.0, 150.0, 150.0, 150.0]


def test_local_runtime_path_capture_uses_one_streaming_budget(
    monkeypatch, tmp_path,
):
    import agent.bestplan_local as bestplan_local
    import agent.bestplan_sandbox as bestplan_sandbox

    launcher = tmp_path / "venv" / "bin" / "python"
    executable = tmp_path / "runtime" / "python3.11"
    pyvenv = launcher.parent.parent / "pyvenv.cfg"
    runtime_root = tmp_path / "runtime" / "lib" / "python3.11"
    for path in (launcher.parent, executable.parent, runtime_root, pyvenv.parent):
        path.mkdir(parents=True, exist_ok=True)
    launcher.symlink_to(executable)
    executable.write_bytes(b"python")
    executable.chmod(0o755)
    pyvenv.write_text("home = /runtime\n", encoding="utf-8")

    budget = SimpleNamespace(deadline=time.monotonic() + 10.0, check=lambda: None)
    seen: list[tuple[Path, object]] = []

    monkeypatch.setattr(
        bestplan_sandbox,
        "pinned_candidate_runtime_paths",
        lambda _path: (runtime_root,),
    )

    def stable_identity(path, identity_budget):
        seen.append((Path(path), identity_budget))
        return {
            "path": str(path),
            "kind": "directory" if Path(path).is_dir() else "file",
            "sha256": hashlib.sha256(str(path).encode()).hexdigest(),
        }

    monkeypatch.setattr(
        bestplan_sandbox, "_stable_artifact_tree_identity", stable_identity,
    )
    pins, _identities = bestplan_local._capture_runtime_read_paths(
        launcher,
        pyvenv,
        budget=budget,
        deadline=time.monotonic() + 100.0,
        cancel_event=None,
    )

    assert {item.path for item in pins} == {pyvenv, runtime_root}
    assert {path for path, _budget in seen} == {pyvenv, runtime_root}
    assert all(identity_budget is budget for _path, identity_budget in seen)


def test_local_python_check_detection_is_host_owned_and_process_fixed(tmp_path):
    from agent.bestplan_local import (
        derive_local_check_plan,
    )

    snapshot = _captured_python_repo(tmp_path, pytest_project=True)
    workspace = Path(snapshot.repo.workspace)
    # The authority must use the admitted Git tree, not this mutable path.
    (workspace / "pyproject.toml").unlink()
    (workspace / "package.json").write_text(
        '{"scripts":{"test":"hostile"}}', encoding="utf-8",
    )

    pytest_nodes = [
        "tests/test_sample.py::test_second",
        "tests/test_sample.py::test_sample",
    ]
    plan = derive_local_check_plan(
        snapshot=snapshot,
        controller_python=Path(sys.executable),
        config={"pytest_nodes": pytest_nodes},
        deadline=time.monotonic() + 30.0,
    )

    commands = plan.commands
    assert len(commands) == 1
    command = commands[0]
    assert command.identifier == "pytest"
    assert Path(command.executable) == Path(sys.executable).resolve()
    assert not Path(command.executable).is_symlink()
    assert command.argv == (
        "-I", "-B", "-m", "pytest", "-q", "--", *pytest_nodes,
    )
    assert command.logical_cwd == "integration"
    assert command.network_allowlist == ()
    assert command.env == (
        ("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1"),
        ("PYTHONHASHSEED", "0"),
        ("__PYVENV_LAUNCHER__", str(Path(sys.executable).absolute())),
    )
    assert command.inputs == ()
    assert command.cache == ()

    runtime_paths = plan.runtime_read_paths
    assert Path(sys.executable).parent.parent / "pyvenv.cfg" in {
        item.path for item in runtime_paths
    }
    assert any("site-packages" in str(item.path) for item in runtime_paths)
    assert len(plan.check_runtime_digest) == 64
    assert plan.pytest_module_path.name == "__init__.py"
    assert any(
        plan.pytest_module_path == item.path
        or item.path in plan.pytest_module_path.parents
        for item in runtime_paths
    )


@pytest.mark.parametrize(
    "config",
    (
        None,
        {},
        {"pytest_nodes": []},
        {"pytest_nodes": "tests/test_sample.py::test_sample"},
        {"pytest_nodes": ["tests/test_sample.py"], "extra": True},
    ),
)
def test_local_check_config_requires_one_exact_pytest_node_list(tmp_path, config):
    from agent.bestplan_local import LocalGoValidationError, derive_local_check_plan

    with pytest.raises(LocalGoValidationError, match="pytest|check config"):
        derive_local_check_plan(
            snapshot=_snapshot(tmp_path),
            controller_python=Path(sys.executable),
            config=config,
        )


def test_local_check_acceptance_binds_exact_nodes_for_each_writable_slice():
    from agent import bestplan_local

    manifest = {
        "slices": [
            {
                "id": "one",
                "read_only": False,
                "acceptance": [
                    "The behavior is correct.",
                    "pytest -q -- tests/test_one.py::test_one",
                ],
            },
            {
                "id": "two",
                "read_only": False,
                "acceptance": [
                    "pytest -q -- tests/test_two.py::test_two "
                    "tests/test_shared.py",
                ],
            },
            {
                "id": "read-only",
                "read_only": True,
                "acceptance": ["Review the exact diff."],
            },
        ],
    }

    assert bestplan_local._local_check_config_from_manifest(manifest) == {
        "pytest_nodes": [
            "tests/test_one.py::test_one",
            "tests/test_two.py::test_two",
            "tests/test_shared.py",
        ],
    }


@pytest.mark.parametrize(
    "acceptance",
    (
        ["The focused tests pass."],
        ["pytest -q tests/test_sample.py"],
        ["pytest -q --"],
        ["pytest -q -- tests/test_sample.py;rm"],
    ),
)
def test_local_check_acceptance_rejects_missing_or_unsafe_exact_command(acceptance):
    from agent import bestplan_local

    with pytest.raises(
        bestplan_local.LocalGoValidationError,
        match="acceptance|pytest",
    ):
        bestplan_local._local_check_config_from_manifest(
            {
                "slices": [
                    {
                        "id": "implementation",
                        "read_only": False,
                        "acceptance": acceptance,
                    },
                ],
            },
        )


def test_local_check_acceptance_fails_before_controller_retention(
    tmp_path, monkeypatch,
):
    from agent import bestplan_local

    monkeypatch.setattr(
        bestplan_local,
        "_retain_local_controller",
        lambda **_kwargs: pytest.fail("controller retention started"),
    )
    with pytest.raises(
        bestplan_local.LocalGoValidationError,
        match="acceptance",
    ):
        bestplan_local.capture_local_execution_inputs(
            snapshot=_snapshot(tmp_path),
            controller_python=Path(sys.executable),
            manifest={
                "slices": [
                    {
                        "id": "implementation",
                        "read_only": False,
                        "acceptance": ["The focused tests pass."],
                    },
                ],
            },
            _controller_checkout=tmp_path,
        )


@pytest.mark.parametrize(
"node",
    (
        "",
        "   ",
        "--collect-only",
        "-q",
        "/tests/test_sample.py",
        "../tests/test_sample.py",
        "./tests/test_sample.py",
        "tests/../test_sample.py",
        "tests//test_sample.py",
        "tests\\test_sample.py",
        "src/test_sample.py",
        "tests/test_sample.txt",
        "tests/test_sample.py::",
        "tests/test_sample.py -k hostile",
        "tests/test_sample.py::test_sample;rm",
        "tests/test_sample.py::test_sample|cat",
        "tests/test_sample.py::test_sample[param/escape]",
    ),
)
def test_local_check_config_rejects_options_and_noncanonical_nodes(tmp_path, node):
    from agent.bestplan_local import LocalGoValidationError, derive_local_check_plan

    with pytest.raises(LocalGoValidationError, match="pytest node"):
        derive_local_check_plan(
            snapshot=_snapshot(tmp_path),
            controller_python=Path(sys.executable),
            config={"pytest_nodes": [node]},
        )


def test_local_check_config_bounds_node_count_and_utf8_bytes(tmp_path):
    from agent.bestplan_local import LocalGoValidationError, derive_local_check_plan

    snapshot = _snapshot(tmp_path)
    cases = (
        {"pytest_nodes": [f"tests/test_{index}.py" for index in range(65)]},
        {"pytest_nodes": [f"tests/{'a' * 16384}.py"]},
    )

    for config in cases:
        with pytest.raises(LocalGoValidationError, match="pytest nodes.*oversized"):
            derive_local_check_plan(
                snapshot=snapshot,
                controller_python=Path(sys.executable),
                config=config,
            )


def test_local_check_detection_rejects_unsupported_project_before_execution(tmp_path):
    from agent.bestplan_local import LocalGoValidationError, derive_local_check_plan

    snapshot = _captured_python_repo(tmp_path, pytest_project=False)

    with pytest.raises(LocalGoValidationError, match="supported exact check"):
        derive_local_check_plan(
            snapshot=snapshot,
            controller_python=Path(sys.executable),
            config={"pytest_nodes": ["tests/test_sample.py::test_sample"]},
        )


def _broker_request(model: str, request_id: str = "turn-00000001"):
    from agent.bestplan_authority_client import BrokerTurnRequest

    body = {
        "max_completion_tokens": 64,
        "messages": [{"role": "user", "content": "do it"}],
        "model": model,
        "stream": False,
    }
    return BrokerTurnRequest(
        request_id=request_id,
        request_json=json.dumps(body, sort_keys=True, separators=(",", ":")),
        max_output_tokens=64,
    )


class _FakeCompletions:
    def __init__(self, model: str, calls: list[dict]):
        self.model = model
        self.calls = calls

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            model_dump=lambda mode="json": {
                "id": "chatcmpl-local",
                "object": "chat.completion",
                "created": 1,
                "model": self.model,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "done",
                            "tool_calls": None,
                        },
                    },
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 1,
                    "total_tokens": 4,
                },
            },
        )


class _ProjectedCompletions(_FakeCompletions):
    def create(self, **kwargs):
        response = super().create(**kwargs)
        body = response.model_dump()
        body["provider_debug"] = {"api_key": "nested-provider-secret"}
        body["choices"][0]["provider_route"] = "https://provider.invalid/v1"
        body["choices"][0]["message"]["credential"] = "nested-message-secret"
        return SimpleNamespace(model_dump=lambda mode="json": body)


def test_local_model_relay_binds_same_model_to_distinct_exact_routes(monkeypatch):
    from agent.bestplan_authority_client import WorkerIdentity
    from agent.bestplan_local import LocalBestplanAuthority

    resolutions: list[dict] = []
    calls: list[dict] = []

    def resolve(provider, model, **kwargs):
        resolutions.append({"provider": provider, "model": model, **kwargs})
        return SimpleNamespace(
            chat=SimpleNamespace(completions=_FakeCompletions(model, calls)),
        ), model

    monkeypatch.setattr("agent.bestplan_local.resolve_provider_client", resolve)
    common = {
        "provider": "custom",
        "model": "same-model",
        "api_mode": "chat_completions",
        "api_key": "parent-only-secret",
        "request_overrides": {"temperature": 0.1},
    }
    first = LocalBestplanAuthority.from_runtime(
        {**common, "base_url": "https://first.example/v1"},
    )
    second = LocalBestplanAuthority.from_runtime(
        {**common, "base_url": "https://second.example/v1"},
    )
    identity = WorkerIdentity(
        pid=123,
        uid=501,
        process_start_id="start-1",
        executable_sha256="d" * 64,
    )

    first_cap = first.register_model_attempt(
        "attempt-one", identity, "same-model", 2, 10_000, 2**31,
    )
    second_cap = second.register_model_attempt(
        "attempt-two", identity, "same-model", 2, 10_000, 2**31,
    )
    first_response = first.model_request(first_cap, _broker_request("same-model"))
    second_response = second.model_request(second_cap, _broker_request("same-model"))

    assert [item["explicit_base_url"] for item in resolutions] == [
        "https://first.example/v1",
        "https://second.example/v1",
    ]
    assert all(item["explicit_api_key"] == "parent-only-secret" for item in resolutions)
    assert first_response.request_id == "turn-00000001"
    assert second_response.request_id == "turn-00000001"
    assert "parent-only-secret" not in first_response.response_json
    assert "first.example" not in first_response.response_json
    assert set(json.loads(first_response.response_json)) == {
        "id", "object", "created", "model", "choices", "usage",
    }


def test_local_authorities_bind_exact_manifest_order_and_runtime_fingerprint():
    from agent.bestplan_local import build_local_authority_bindings
    from tools.delegate_tool import _ordered_bestplan_authority_clients

    common = {
        "provider": "custom",
        "model": "same-model",
        "api_mode": "chat_completions",
        "api_key": "secret",
        "request_overrides": {},
        "no_auth": False,
    }
    runtimes = [
        {
            **common,
            "base_url": "https://first.example/v1",
            "runtime_fingerprint": "1" * 64,
        },
        {
            **common,
            "base_url": "https://second.example/v1",
            "runtime_fingerprint": "2" * 64,
        },
    ]

    bindings = build_local_authority_bindings(runtimes)
    ordered = _ordered_bestplan_authority_clients(
        runtimes, authority_client=None, authority_bindings=bindings,
    )

    assert [item.position for item in bindings] == [0, 1]
    assert [item.runtime_fingerprint for item in bindings] == ["1" * 64, "2" * 64]
    assert ordered == tuple(item.authority for item in bindings)
    assert ordered[0] is not ordered[1]
    with pytest.raises(ValueError, match="authority"):
        _ordered_bestplan_authority_clients(
            runtimes,
            authority_client=None,
            authority_bindings=tuple(reversed(bindings)),
        )


def test_local_model_relay_reserves_tokens_before_provider_call(monkeypatch):
    from agent.bestplan_authority_client import AuthorityProtocolError, WorkerIdentity
    from agent.bestplan_local import LocalBestplanAuthority

    provider_calls: list[dict] = []
    monkeypatch.setattr(
        "agent.bestplan_local.resolve_provider_client",
        lambda provider, model, **kwargs: (
            SimpleNamespace(
                chat=SimpleNamespace(
                    completions=_FakeCompletions(model, provider_calls),
                ),
            ),
            model,
        ),
    )
    authority = LocalBestplanAuthority.from_runtime({
        "provider": "custom",
        "model": "bound-model",
        "base_url": "https://example.test/v1",
        "api_mode": "chat_completions",
        "api_key": "secret",
        "no_auth": False,
    })
    identity = WorkerIdentity(
        pid=123,
        uid=501,
        process_start_id="start-1",
        executable_sha256="d" * 64,
    )
    capability = authority.register_model_attempt(
        "attempt", identity, "bound-model", 1, 64, 2**31,
    )

    with pytest.raises(AuthorityProtocolError, match="token budget"):
        authority.model_request(capability, _broker_request("bound-model"))
    assert provider_calls == []


def test_local_model_relay_projects_exact_provider_neutral_response(monkeypatch):
    from agent.bestplan_authority_client import WorkerIdentity
    from agent.bestplan_local import LocalBestplanAuthority

    monkeypatch.setattr(
        "agent.bestplan_local.resolve_provider_client",
        lambda provider, model, **kwargs: (
            SimpleNamespace(
                chat=SimpleNamespace(
                    completions=_ProjectedCompletions(model, []),
                ),
            ),
            model,
        ),
    )
    authority = LocalBestplanAuthority.from_runtime({
        "provider": "custom",
        "model": "bound-model",
        "base_url": "https://example.test/v1",
        "api_mode": "chat_completions",
        "api_key": "secret",
        "no_auth": False,
    })
    identity = WorkerIdentity(
        pid=123,
        uid=501,
        process_start_id="start-1",
        executable_sha256="d" * 64,
    )
    capability = authority.register_model_attempt(
        "attempt", identity, "bound-model", 1, 10_000, 2**31,
    )

    response = authority.model_request(capability, _broker_request("bound-model"))
    body = json.loads(response.response_json)

    assert set(body["choices"][0]) == {"index", "finish_reason", "message"}
    assert set(body["choices"][0]["message"]) == {"role", "content", "tool_calls"}
    assert "provider" not in response.response_json
    assert "credential" not in response.response_json


def test_local_model_relay_explicit_no_auth_never_uses_credential_fallback(monkeypatch):
    from agent.bestplan_authority_client import WorkerIdentity
    from agent.bestplan_local import LocalBestplanAuthority

    resolutions: list[dict] = []

    def resolve(provider, model, **kwargs):
        resolutions.append(kwargs)
        return SimpleNamespace(
            chat=SimpleNamespace(completions=_FakeCompletions(model, [])),
        ), model

    monkeypatch.setattr("agent.bestplan_local.resolve_provider_client", resolve)
    with pytest.raises(Exception, match="api_key|no_auth"):
        LocalBestplanAuthority.from_runtime({
            "provider": "custom",
            "model": "bound-model",
            "base_url": "https://example.test/v1",
            "api_mode": "chat_completions",
            "api_key": "",
            "no_auth": False,
        })
    authority = LocalBestplanAuthority.from_runtime({
        "provider": "custom",
        "model": "bound-model",
        "base_url": "https://example.test/v1",
        "api_mode": "chat_completions",
        "api_key": "",
        "no_auth": True,
    })
    identity = WorkerIdentity(
        pid=123,
        uid=501,
        process_start_id="start-1",
        executable_sha256="d" * 64,
    )
    capability = authority.register_model_attempt(
        "attempt", identity, "bound-model", 1, 10_000, 2**31,
    )
    authority.model_request(capability, _broker_request("bound-model"))

    assert resolutions[0]["explicit_api_key"] == "hermes-bestplan-no-auth"
    with pytest.raises(Exception, match="no_auth"):
        LocalBestplanAuthority.from_runtime({
            "provider": "openai-codex",
            "model": "bound-model",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_mode": "codex_responses",
            "api_key": "",
            "no_auth": True,
        })


def test_local_model_relay_rejects_secret_request_overrides():
    from agent.bestplan_local import LocalBestplanAuthority, LocalGoValidationError

    with pytest.raises(LocalGoValidationError, match="override"):
        LocalBestplanAuthority.from_runtime({
            "provider": "custom",
            "model": "bound-model",
            "base_url": "https://example.test/v1",
            "api_mode": "chat_completions",
            "api_key": "parent-secret",
            "no_auth": False,
            "request_overrides": {
                "extra_headers": {"Authorization": "Bearer parent-secret"},
            },
        })


def test_protocol2_async_runtime_metadata_is_an_exact_nonsecret_allowlist():
    from tools.delegate_tool import _bestplan_async_runtime_metadata

    runtime = {
        "route": "code_worker",
        "provider": "custom",
        "model": "worker-model",
        "api_mode": "chat_completions",
        "runtime_fingerprint": "1" * 64,
        "sandbox_backend": "bestplan-candidate",
        "sandbox_policy_digest": "2" * 64,
        "candidate_host_runtime_digest": "3" * 64,
        "candidate_policy_version": 1,
        "candidate_request_budget": 2,
        "candidate_token_budget": 3,
        "candidate_max_iterations": 4,
        "candidate_max_output_tokens": 5,
        "candidate_timeout_seconds": 6.0,
        "candidate_capability_ttl_seconds": 7.0,
        "base_url": "https://must-not-cross.example/v1",
        "endpoint": "https://must-not-cross.example/v1",
        "api_key": "must-not-cross",
        "no_auth": False,
        "request_overrides": {"authorization": "must-not-cross"},
        "runtime_identity": {"endpoint": "https://must-not-cross.example/v1"},
        "unexpected": "must-not-cross",
    }

    projected = _bestplan_async_runtime_metadata(
        runtime, candidate_toolsets=("file",), execution_protocol=2,
    )

    assert set(projected) == {
        "route",
        "provider",
        "model",
        "api_mode",
        "runtime_fingerprint",
        "sandbox_backend",
        "sandbox_policy_digest",
        "candidate_host_runtime_digest",
        "candidate_policy_version",
        "candidate_request_budget",
        "candidate_token_budget",
        "candidate_max_iterations",
        "candidate_max_output_tokens",
        "candidate_timeout_seconds",
        "candidate_capability_ttl_seconds",
        "toolsets",
        "bestplan_toolsets",
    }
    assert projected["toolsets"] == ["file"]
    assert projected["bestplan_toolsets"] == ["file"]
    assert "must-not-cross" not in json.dumps(projected)


def test_local_model_relay_revocation_expiry_and_model_binding(monkeypatch):
    from agent.bestplan_authority_client import (
        AuthorityProtocolError,
        WorkerIdentity,
    )
    from agent.bestplan_local import LocalBestplanAuthority

    monkeypatch.setattr(
        "agent.bestplan_local.resolve_provider_client",
        lambda provider, model, **kwargs: (
            SimpleNamespace(
                chat=SimpleNamespace(
                    completions=_FakeCompletions(model, []),
                ),
            ),
            model,
        ),
    )
    authority = LocalBestplanAuthority.from_runtime({
        "provider": "custom",
        "model": "bound-model",
        "base_url": "https://example.test/v1",
        "api_mode": "chat_completions",
        "api_key": "secret",
    })
    identity = WorkerIdentity(
        pid=123,
        uid=501,
        process_start_id="start-1",
        executable_sha256="d" * 64,
    )

    with pytest.raises(AuthorityProtocolError, match="model"):
        authority.register_model_attempt(
            "wrong-model", identity, "other-model", 1, 10, 2**31,
        )

    capability = authority.register_model_attempt(
        "attempt", identity, "bound-model", 1, 10, 2**31,
    )
    authority.revoke_model_attempt(capability)
    with pytest.raises(AuthorityProtocolError, match="revoked"):
        authority.model_request(capability, _broker_request("bound-model"))

    expired = authority.register_model_attempt(
        "expired", identity, "bound-model", 1, 10, 1,
    )
    with pytest.raises(AuthorityProtocolError, match="expired"):
        authority.model_request(expired, _broker_request("bound-model"))
