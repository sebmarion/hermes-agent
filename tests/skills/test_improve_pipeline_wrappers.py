"""Tests for operational wrapper failure isolation."""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest


WRAPPER = Path(__file__).resolve().parents[2] / "scripts/upstream_merge_wrapper.py"
IMPROVE_WRAPPER = Path(__file__).resolve().parents[2] / "scripts/improve_loop_wrapper.py"
ACTIVATION_VERIFIER = Path(__file__).resolve().parents[2] / "scripts/verify_upstream_activation.sh"


def _load_wrapper():
    spec = importlib.util.spec_from_file_location("upstream_merge_wrapper_test", WRAPPER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_improve_wrapper():
    spec = importlib.util.spec_from_file_location("improve_loop_wrapper_test", IMPROVE_WRAPPER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_notification_timeout_does_not_escape_merge_wrapper(monkeypatch) -> None:
    wrapper = _load_wrapper()

    def timeout(*args, **kwargs):
        raise wrapper.subprocess.TimeoutExpired(cmd="notify", timeout=90)

    monkeypatch.setattr(wrapper.subprocess, "run", timeout)
    wrapper._notify("halted", "merge halted")


def test_upstream_gate_uses_bounded_relevant_suites() -> None:
    wrapper = _load_wrapper()
    command = wrapper._build_command()
    test_paths = [
        command[i + 1]
        for i, value in enumerate(command[:-1])
        if value == "--test" and command[i + 1] != str(wrapper.PYTHON)
    ]
    assert "tests/skills" in test_paths
    assert "tests/gateway/test_scale_to_zero_watcher.py" in test_paths
    assert "tests/plugins/test_teams_pipeline_plugin.py" in test_paths
    assert "tests/tools/test_memory_tool.py" in test_paths
    assert "tests" not in test_paths


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def _init_repo(repo: Path) -> str:
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "file.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return _git(repo, "rev-parse", "HEAD")


def test_build_command_targets_supplied_isolated_repo(tmp_path: Path) -> None:
    wrapper = _load_wrapper()
    isolated = tmp_path / "isolated"
    command = wrapper._build_command(isolated)

    assert command[command.index("--repo") + 1] == str(isolated)
    assert str(isolated / "optional-skills/research/darwinian-evolver/labs/scripts/merge_upstream.py") in command


def test_canonical_clean_guard_ignores_only_deployment_plugin_untracked_files(tmp_path: Path) -> None:
    wrapper = _load_wrapper()
    repo = tmp_path / "repo"
    _init_repo(repo)
    deployment_file = repo / "plugins/hermes-bestplan/plugin.yaml"
    deployment_file.parent.mkdir(parents=True)
    deployment_file.write_text("deployment-only\n")
    assert wrapper._is_clean(repo)

    (repo / "unexpected.txt").write_text("unexpected\n")
    assert not wrapper._is_clean(repo)


def test_isolated_clone_has_independent_refs_and_leaves_source_unchanged(tmp_path: Path) -> None:
    wrapper = _load_wrapper()
    source = tmp_path / "source"
    source_head = _init_repo(source)
    upstream = tmp_path / "upstream.git"
    fork = tmp_path / "fork.git"
    subprocess.run(["git", "clone", "--bare", str(source), str(upstream)], check=True, capture_output=True)
    subprocess.run(["git", "clone", "--bare", str(source), str(fork)], check=True, capture_output=True)

    run_parent, run_repo = wrapper._create_isolated_repo(
        source_head,
        source_repo=source,
        run_root=tmp_path / "runs",
        upstream_url=str(upstream),
        fork_url=str(fork),
    )
    try:
        assert _git(run_repo, "rev-parse", "HEAD") == source_head
        assert _git(source, "rev-parse", "HEAD") == source_head
        assert _git(source, "status", "--porcelain") == ""
        run_common = Path(_git(run_repo, "rev-parse", "--git-common-dir"))
        source_common = Path(_git(source, "rev-parse", "--git-common-dir"))
        if not run_common.is_absolute():
            run_common = run_repo / run_common
        if not source_common.is_absolute():
            source_common = source / source_common
        assert run_common.resolve() != source_common.resolve()
    finally:
        shutil.rmtree(run_parent, ignore_errors=True)
    subprocess.run(["git", "fsck", "--no-progress"], cwd=source, check=True, capture_output=True)


def test_canonical_promotion_is_fast_forward_only(tmp_path: Path, monkeypatch) -> None:
    wrapper = _load_wrapper()
    canonical = tmp_path / "canonical"
    base = _init_repo(canonical)
    fork = tmp_path / "fork.git"
    subprocess.run(["git", "clone", "--bare", str(canonical), str(fork)], check=True, capture_output=True)
    _git(canonical, "remote", "add", "sebmarion-fork", str(fork))

    worker = tmp_path / "worker"
    subprocess.run(["git", "clone", str(fork), str(worker)], check=True, capture_output=True)
    _git(worker, "config", "user.name", "test")
    _git(worker, "config", "user.email", "test@example.invalid")
    (worker / "file.txt").write_text("candidate\n")
    _git(worker, "add", ".")
    _git(worker, "commit", "-qm", "candidate")
    candidate = _git(worker, "rev-parse", "HEAD")
    _git(worker, "push", "origin", "HEAD:main")

    monkeypatch.setattr(wrapper, "REPO", canonical)
    assert wrapper._promote_canonical(base, candidate) == candidate
    assert _git(canonical, "rev-parse", "HEAD") == candidate
    assert _git(canonical, "status", "--porcelain") == ""


def test_canonical_install_sync_uses_locked_project(tmp_path: Path, monkeypatch) -> None:
    wrapper = _load_wrapper()
    calls = []
    probe_snapshot = {}

    assert "CronTickYielded" in wrapper.INSTALLED_IMPORT_PROBE
    assert "is_relative_to" in wrapper.INSTALLED_IMPORT_PROBE

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[0] == str(wrapper.PYTHON):
            probe_snapshot.update(
                {
                    "mode": kwargs["cwd"].stat().st_mode & 0o777,
                    "env": kwargs["env"],
                    "cwd": kwargs["cwd"],
                }
            )
        return subprocess.CompletedProcess(argv, 0, stdout="synced", stderr="")

    monkeypatch.setattr(wrapper.subprocess, "run", fake_run)

    wrapper._sync_canonical_install(tmp_path)

    assert calls == [
        (
            [
                str(wrapper.UV),
                "sync",
                "--locked",
                "--extra",
                "dev",
                "--extra",
                "messaging",
            ],
            {
                "cwd": tmp_path,
                "capture_output": True,
                "text": True,
                "timeout": wrapper.INSTALL_SYNC_TIMEOUT_SECONDS,
            },
        ),
        (
            [str(wrapper.PYTHON), "-I", "-c", wrapper.INSTALLED_IMPORT_PROBE],
            {
                "cwd": probe_snapshot["cwd"],
                "capture_output": True,
                "env": probe_snapshot["env"],
                "text": True,
                "timeout": 60,
            },
        ),
    ]
    assert probe_snapshot["mode"] == 0o700
    assert probe_snapshot["cwd"] != Path("/tmp")
    assert "PYTHONPATH" not in probe_snapshot["env"]
    assert "PYTHONHOME" not in probe_snapshot["env"]


def test_canonical_install_sync_fails_closed_on_import_probe(
    tmp_path: Path, monkeypatch
) -> None:
    wrapper = _load_wrapper()
    results = iter(
        [
            subprocess.CompletedProcess([], 0, stdout="synced", stderr=""),
            subprocess.CompletedProcess([], 1, stdout="", stderr="missing watchdog"),
        ]
    )
    monkeypatch.setattr(wrapper.subprocess, "run", lambda *_a, **_k: next(results))

    with pytest.raises(wrapper.WrapperError, match="installed import probe failed"):
        wrapper._sync_canonical_install(tmp_path)


def test_canonical_promotion_syncs_install_before_returning(monkeypatch) -> None:
    wrapper = _load_wrapper()
    order = []
    monkeypatch.setattr(
        wrapper,
        "_promote_canonical",
        lambda expected, published: order.append(("promote", expected, published))
        or published,
    )
    monkeypatch.setattr(
        wrapper,
        "_sync_canonical_install",
        lambda repo: order.append(("sync", repo)),
    )

    assert wrapper._promote_and_sync_canonical("old", "new") == "new"
    assert order == [
        ("promote", "old", "new"),
        ("sync", wrapper.REPO),
    ]


def test_recovery_retries_install_sync_before_starting_a_new_merge(
    tmp_path: Path, monkeypatch
) -> None:
    wrapper = _load_wrapper()
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(wrapper, "STATE", state)
    monkeypatch.setattr(wrapper, "LOCK", state / "lock")
    monkeypatch.setattr(wrapper, "_prune_stale_runs", lambda: None)
    monkeypatch.setattr(wrapper, "_assert_canonical", lambda: "0" * 40)
    monkeypatch.setattr(wrapper, "_recover_published_activation", lambda: "0" * 40)
    monkeypatch.setattr(wrapper, "_git", lambda *_a, **_k: "0" * 40)
    monkeypatch.setattr(
        wrapper,
        "_sync_canonical_install",
        lambda _repo: (_ for _ in ()).throw(wrapper.WrapperError("sync retry failed")),
    )
    monkeypatch.setattr(
        wrapper,
        "_create_isolated_repo",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("merge must not start before recovery sync succeeds")
        ),
    )
    monkeypatch.setattr(wrapper, "_notify", lambda *_a, **_k: None)

    assert wrapper.main() == 1
    report = (state / "last-upstream-merge.txt").read_text()
    assert "sync retry failed" in report
    assert "canonical checkout was not mutated" in report


def test_post_promotion_sync_failure_reports_canonical_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    wrapper = _load_wrapper()
    state = tmp_path / "state"
    run_parent = tmp_path / "run"
    run_repo = run_parent / "repo"
    run_repo.mkdir(parents=True)
    state.mkdir()
    sync_calls = []
    monkeypatch.setattr(wrapper, "STATE", state)
    monkeypatch.setattr(wrapper, "LOCK", state / "lock")
    monkeypatch.setattr(wrapper, "_prune_stale_runs", lambda: None)
    monkeypatch.setattr(wrapper, "_assert_canonical", lambda: "old")
    monkeypatch.setattr(wrapper, "_recover_published_activation", lambda: "old")

    def sync(_repo):
        sync_calls.append(1)
        if len(sync_calls) == 2:
            raise wrapper.WrapperError("post-promotion sync failed")

    monkeypatch.setattr(wrapper, "_sync_canonical_install", sync)
    monkeypatch.setattr(
        wrapper, "_create_isolated_repo", lambda *_a, **_k: (run_parent, run_repo)
    )
    monkeypatch.setattr(
        wrapper.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, stdout="ok", stderr=""),
    )
    monkeypatch.setattr(wrapper, "_load_sync_state", lambda: {"published_sha": "a" * 40})
    monkeypatch.setattr(wrapper, "_promote_canonical", lambda *_a, **_k: "a" * 40)
    monkeypatch.setattr(wrapper, "_notify", lambda *_a, **_k: None)

    assert wrapper.main() == 1
    report = (state / "last-upstream-merge.txt").read_text()
    assert "post-promotion sync failed" in report
    assert "canonical checkout advanced" in report
    assert "activation was withheld" in report


def test_recovery_failure_after_fast_forward_reports_actual_canonical_head(
    tmp_path: Path, monkeypatch
) -> None:
    wrapper = _load_wrapper()
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(wrapper, "STATE", state)
    monkeypatch.setattr(wrapper, "LOCK", state / "lock")
    monkeypatch.setattr(wrapper, "_prune_stale_runs", lambda: None)
    monkeypatch.setattr(wrapper, "_assert_canonical", lambda: "0" * 40)
    monkeypatch.setattr(
        wrapper,
        "_recover_published_activation",
        lambda: (_ for _ in ()).throw(wrapper.WrapperError("post-ff verification failed")),
    )
    monkeypatch.setattr(wrapper, "_git", lambda *_a, **_k: "a" * 40)
    monkeypatch.setattr(wrapper, "_notify", lambda *_a, **_k: None)

    assert wrapper.main() == 1
    report = (state / "last-upstream-merge.txt").read_text()
    assert "post-ff verification failed" in report
    assert f"canonical checkout advanced to {'a' * 40}" in report
    assert "activation was withheld" in report


@pytest.mark.parametrize("actual_head", ["", "z" * 40])
def test_failure_with_invalid_canonical_head_reports_mutation_unknown(
    tmp_path: Path, monkeypatch, actual_head: str
) -> None:
    wrapper = _load_wrapper()
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(wrapper, "STATE", state)
    monkeypatch.setattr(wrapper, "LOCK", state / "lock")
    monkeypatch.setattr(wrapper, "_prune_stale_runs", lambda: None)
    monkeypatch.setattr(wrapper, "_assert_canonical", lambda: "0" * 40)
    monkeypatch.setattr(
        wrapper,
        "_recover_published_activation",
        lambda: (_ for _ in ()).throw(wrapper.WrapperError("recovery failed")),
    )
    monkeypatch.setattr(wrapper, "_git", lambda *_a, **_k: actual_head)
    monkeypatch.setattr(wrapper, "_notify", lambda *_a, **_k: None)

    assert wrapper.main() == 1
    report = (state / "last-upstream-merge.txt").read_text()
    assert "canonical checkout mutation status is unknown" in report
    assert "activation was withheld" in report
    assert "canonical checkout was not mutated" not in report


def test_updater_timeouts_leave_systemd_activation_headroom() -> None:
    wrapper = _load_wrapper()
    service = WRAPPER.parents[1] / "scripts/hermes-upstream-merge.service"
    timeout_line = next(
        line for line in service.read_text().splitlines() if line.startswith("TimeoutStartSec=")
    )
    service_timeout = int(timeout_line.split("=", 1)[1])

    assert wrapper.ACTIVATION_HEADROOM_SECONDS >= 1500
    assert wrapper.SYSTEMD_TIMEOUT_SECONDS == service_timeout
    assert (
        wrapper.MERGER_TIMEOUT_SECONDS
        + (2 * wrapper.INSTALL_SYNC_TIMEOUT_SECONDS)
        + wrapper.NOTIFY_TIMEOUT_SECONDS
        + wrapper.ACTIVATION_HEADROOM_SECONDS
        <= wrapper.SYSTEMD_TIMEOUT_SECONDS
    )


def test_crash_after_publish_recovers_only_from_matching_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    wrapper = _load_wrapper()
    canonical = tmp_path / "canonical"
    base = _init_repo(canonical)
    fork = tmp_path / "fork.git"
    subprocess.run(["git", "clone", "--bare", str(canonical), str(fork)], check=True, capture_output=True)
    _git(canonical, "remote", "add", "sebmarion-fork", str(fork))

    worker = tmp_path / "worker"
    subprocess.run(["git", "clone", str(fork), str(worker)], check=True, capture_output=True)
    _git(worker, "config", "user.name", "test")
    _git(worker, "config", "user.email", "test@example.invalid")
    (worker / "file.txt").write_text("published-before-crash\n")
    _git(worker, "add", ".")
    _git(worker, "commit", "-qm", "published")
    published = _git(worker, "rev-parse", "HEAD")
    _git(worker, "push", "origin", "HEAD:main")

    state = tmp_path / "state"
    state.mkdir()
    (state / "upstream-sync.json").write_text(json.dumps({"published_sha": published}))
    monkeypatch.setattr(wrapper, "REPO", canonical)
    monkeypatch.setattr(wrapper, "STATE", state)

    assert _git(canonical, "rev-parse", "HEAD") == base
    assert wrapper._recover_published_activation() == published
    assert _git(canonical, "rev-parse", "HEAD") == published
    assert _git(canonical, "status", "--porcelain") == ""


def test_improve_loop_child_path_contains_ocr_and_hermes_tool_dirs(tmp_path, monkeypatch) -> None:
    wrapper = _load_improve_wrapper()
    node = tmp_path / "node/bin"
    local = tmp_path / "local/bin"
    python = tmp_path / "venv/bin"
    for directory in (node, local, python): directory.mkdir(parents=True)
    command = local / "ocr"
    command.write_text("#!/bin/sh\nexit 0\n")
    command.chmod(0o755)
    missing = tmp_path / "not-installed"
    monkeypatch.setattr(wrapper, "TOOL_PATHS", (python, local, node, missing))
    child_env = wrapper._child_env()
    child_path = child_env["PATH"].split(wrapper.os.pathsep)
    assert child_path[:3] == [str(python), str(local), str(node)]
    assert str(missing) not in child_path
    assert shutil.which("ocr", path=child_env["PATH"]) == str(command)


def test_improve_loop_allows_one_full_bounded_model_batch() -> None:
    wrapper = _load_improve_wrapper()

    assert wrapper.IMPROVE_TIMEOUT_SECONDS >= 3600


def test_activation_verifier_delegates_to_dual_repo_coordinator() -> None:
    script = ACTIVATION_VERIFIER.read_text()

    assert "exec /usr/local/libexec/hermes-deployment-coordinator.py" in script
    assert "--activate --reason upstream-merge" in script
    assert "systemctl reload hermes-gateway.service" not in script
