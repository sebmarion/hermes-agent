from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent.bestplan_state import (
    BESTPLAN_ENVELOPE_END,
    BESTPLAN_ENVELOPE_START,
    BaselineFingerprintError,
    BestplanStore,
    PlanState,
    capture_bestplan_response,
    compute_baseline_fingerprint,
    run_planning_only_bestplan_turn,
    unsupported_host_bestplan_after_model,
    unsupported_host_bestplan_before_model,
)
from agent.execution_plan import compile_execution_plan


def _git_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "tracked.txt").write_text("base", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True)
    return path


def _manifest(workspace: str, *, review: bool = False) -> dict:
    return {
        "version": 1,
        "mode": "sota" if review else "delegate",
        "risk": "high" if review else "low",
        "slices": [{
            "id": "slice-a",
            "kind": "review" if review else "implement",
            "goal": "Review only" if review else "Implement safely",
            "depends_on": [],
            "capability": "frontier_review" if review else "fast_fallback",
            "workspace": workspace,
            "allowed_paths": [] if review else ["allowed/"],
            "read_only": review,
            "expected_artifacts": ["review.md" if review else "allowed/result.txt"],
            "acceptance": ["hostile writes are denied"],
        }],
        "merge_policy": "Explicit verification only.",
        "stop_condition": "Acceptance passes.",
        "escalation_predicates": ["security_sensitive_request"],
    }


def _envelope(manifest: dict) -> str:
    return (
        f"{BESTPLAN_ENVELOPE_START}\n"
        + json.dumps({"version": 1, "manifest": manifest}, sort_keys=True)
        + f"\n{BESTPLAN_ENVELOPE_END}"
    )


def test_unsupported_host_strips_executable_envelope_and_blocks_followup_go(tmp_path):
    workspace = str(tmp_path.resolve())
    response = "advisory\n" + _envelope(_manifest(workspace))
    result = {
        "final_response": response,
        "messages": [{"role": "assistant", "content": response}],
    }
    hardened = unsupported_host_bestplan_after_model(
        result, invocation_message="/bestplan do it", host_name="tui"
    )
    assert BESTPLAN_ENVELOPE_START not in hardened["final_response"]
    assert "planning-only" in hardened["final_response"].lower()
    assert hardened["bestplan_capture"]["executable"] is False

    blocked = unsupported_host_bestplan_before_model(
        "go", conversation_history=hardened["messages"], host_name="tui"
    )
    assert blocked is not None
    assert blocked.resolved is True
    assert blocked.status == "unsupported_host"

    assert unsupported_host_bestplan_before_model(
        "go", conversation_history=[{"role": "assistant", "content": "ordinary chat"}], host_name="tui"
    ) is None


def test_shared_production_ingress_calls_model_only_for_unblocked_turns(tmp_path):
    calls = []
    ordinary = run_planning_only_bestplan_turn(
        invocation_message="go",
        conversation_history=[{"role": "assistant", "content": "ordinary chat"}],
        host_name="tui",
        host_agent=None,
        run_model_turn=lambda: calls.append("ordinary") or {
            "final_response": "model handled go", "messages": [],
        },
    )
    assert ordinary["final_response"] == "model handled go"
    assert calls == ["ordinary"]

    planned = run_planning_only_bestplan_turn(
        invocation_message="/bestplan do it",
        conversation_history=[],
        host_name="tui",
        host_agent=None,
        run_model_turn=lambda: calls.append("plan") or {
            "final_response": _envelope(_manifest(str(tmp_path))),
            "messages": [{"role": "assistant", "content": _envelope(_manifest(str(tmp_path)))}],
        },
    )
    assert BESTPLAN_ENVELOPE_START not in planned["final_response"]
    blocked = run_planning_only_bestplan_turn(
        invocation_message="go",
        conversation_history=planned["messages"],
        host_name="tui",
        host_agent=None,
        run_model_turn=lambda: calls.append("must-not-run") or {},
    )
    assert blocked["host_ingress"]["status"] == "unsupported_host"
    assert calls == ["ordinary", "plan"]


def test_ignored_regular_file_fails_closed_and_never_silently_changes_baseline(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / ".gitignore").write_text("ignored.cfg\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "ignore config"], cwd=repo, check=True)
    (repo / "ignored.cfg").write_text("approved input", encoding="utf-8")
    with pytest.raises(BaselineFingerprintError, match="ignored regular file"):
        compute_baseline_fingerprint(str(repo))


def test_authoritative_approval_renders_artifacts_and_sandbox_identity(tmp_path):
    repo = _git_repo(tmp_path)
    store = BestplanStore(db_path=tmp_path / "state.db")
    capture = capture_bestplan_response(
        _envelope(_manifest(str(repo))),
        session_id="s", profile="coder", workspace=str(repo),
        baseline_fingerprint="baseline", store=store,
    )
    assert capture.executable
    assert "expected artifacts: allowed/result.txt" in capture.response
    assert "sandbox backend:" in capture.response
    assert "sandbox policy digest:" in capture.response


@pytest.mark.skipif(sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists(), reason="macOS sandbox-exec required")
def test_real_macos_sandbox_denies_shell_python_symlink_chmod_and_original_checkout_writes(tmp_path):
    from agent.bestplan_sandbox import (
        BestplanSandboxUnavailable,
        create_bestplan_sandbox_launch,
        sandbox_backend_identity,
    )

    original = tmp_path / "original"
    isolated = tmp_path / "isolated"
    outside = tmp_path / "outside"
    for path in (original, isolated / "allowed", outside):
        path.mkdir(parents=True)
    (outside / "target.txt").write_text("safe", encoding="utf-8")
    (isolated / "allowed" / "escape-link").symlink_to(
        outside, target_is_directory=True,
    )
    runtime = isolated / ".bestplan-runtime"
    runtime.mkdir()

    identity = sandbox_backend_identity(
        workspace=isolated, allowed_paths=["allowed/"], read_only=False,
    )
    if identity["backend"] == "unavailable":
        with pytest.raises(BestplanSandboxUnavailable):
            create_bestplan_sandbox_launch(
                workspace=isolated, allowed_paths=["allowed/"],
                read_only=False, runtime_dir=runtime,
            )
        pytest.skip("nested test runner cannot apply sandbox-exec; fail-closed verified")

    with create_bestplan_sandbox_launch(
        workspace=isolated,
        allowed_paths=["allowed/"],
        read_only=False,
        runtime_dir=runtime,
    ) as launch:
        assert launch.run(["/bin/sh", "-c", "printf ok > allowed/result.txt"]).returncode == 0
        attacks = [
            ["/bin/sh", "-c", f"printf bad > {original / 'pwned.txt'}"],
            ["/bin/sh", "-c", f"cd {outside} && printf bad > cwd.txt"],
            [sys.executable, "-c", f"from pathlib import Path; Path({str(outside / 'python.txt')!r}).write_text('bad')"],
            ["/bin/sh", "-c", "printf bad > allowed/escape-link/symlink.txt"],
            ["/bin/sh", "-c", f"chmod 777 {outside} && printf bad > {outside / 'chmod.txt'}"],
        ]
        for command in attacks:
            assert launch.run(command).returncode != 0

    assert not (original / "pwned.txt").exists()
    assert not (outside / "cwd.txt").exists()
    assert not (outside / "python.txt").exists()
    assert not (outside / "symlink.txt").exists()
    assert not (outside / "chmod.txt").exists()


def test_detached_sandbox_preserves_approved_git_subdirectory(tmp_path, monkeypatch):
    from tools.delegate_tool import _bestplan_sandbox_workspace

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    project = repo / "services" / "api"
    project.mkdir(parents=True)
    (project / "tracked.txt").write_text("base", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    home = tmp_path / "hermes-home"
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: home)

    detached = _bestplan_sandbox_workspace(str(project), "bp-subdir")
    expected_root = home / "bestplan" / "worktrees" / "bp-subdir"
    assert detached.relative_to(expected_root) == Path("services/api")
    assert (detached / "tracked.txt").read_text(encoding="utf-8") == "base"


def test_fast_completion_evidence_wins_over_late_dispatch_write(tmp_path):
    workspace = str(tmp_path.resolve())
    store = BestplanStore(db_path=tmp_path / "state.db")
    plan_id = store.create_plan(
        "do it", compile_execution_plan(_manifest(workspace)),
        session_id="s", profile="coder", workspace=workspace,
        baseline_fingerprint="base",
    )
    runtimes = [{"route": "code_worker", "provider": "p", "model": "m", "runtime_fingerprint": "fp"}]
    assert store.prepare_dispatch_intent(
        plan_id, "base", resolved_runtimes=runtimes,
        session_id="s", profile="coder", workspace=workspace,
    )
    assert store.begin_dispatch_attempt(plan_id)
    assert store.mark_completed_unverified(plan_id, {"delegation_id": f"bestplan-{plan_id}"})
    assert store.record_dispatch(plan_id, delegation_ids=[f"bestplan-{plan_id}"])
    row = store.get_plan(plan_id)
    assert row["state"] == PlanState.COMPLETED_UNVERIFIED
    assert row["dispatch_state"] == "terminal"


def test_cli_stack_binds_capability_profile_and_home(tmp_path):
    from agent.bestplan_state import bind_bestplan_delivery_context, try_resolve_go
    from gateway.session_context import get_delivery_context_identity
    from hermes_constants import get_hermes_home

    home = tmp_path / "profile-home"
    workspace = str(tmp_path.resolve())
    store = BestplanStore(db_path=tmp_path / "cli-state.db")
    plan_id = store.create_plan(
        "do it", compile_execution_plan(_manifest(workspace)),
        session_id="cli-session", profile="coder", workspace=workspace,
        baseline_fingerprint="base",
    )
    before = get_hermes_home()
    with bind_bestplan_delivery_context(
        session_key="cli-session", session_id="cli-session",
        profile="coder", hermes_home=home,
    ):
        identity = get_delivery_context_identity()
        assert identity["capability_version"] == 1
        assert identity["profile"] == "coder"
        assert Path(identity["hermes_home"]) == home.resolve()
        assert get_hermes_home() == home
        dispatched = try_resolve_go(
            "go", session_id="cli-session", profile="coder", workspace=workspace,
            baseline_fingerprint="base", parent_agent=object(), store=store,
            config={"autonomy": {"go_enabled": True}},
            runtime_resolver=lambda _tasks, _parent: [{
                "route": "code_worker", "provider": "p", "model": "m",
                "runtime_fingerprint": "fp",
            }],
            strict_dispatcher=lambda **kwargs: {
                "status": "dispatched", "delegation_id": kwargs["dispatch_id"],
            },
        )
        assert dispatched.status == "waiting"
        assert store.get_plan(plan_id)["dispatch_state"] == "scheduled"
    assert get_hermes_home() == before
    assert "bind_bestplan_delivery_context" in (Path(__file__).resolve().parents[2] / "cli.py").read_text(encoding="utf-8")


def test_startup_reconciles_scheduled_and_terminal_tracker_phases(tmp_path):
    workspace = str(tmp_path.resolve())
    db_path = tmp_path / "state.db"
    store = BestplanStore(db_path=db_path)
    plan_id = store.create_plan(
        "do it", compile_execution_plan(_manifest(workspace)),
        session_id="s", profile="coder", workspace=workspace,
        baseline_fingerprint="base",
    )
    store.prepare_dispatch_intent(
        plan_id, "base",
        resolved_runtimes=[{"runtime_fingerprint": "fp"}],
        session_id="s", profile="coder", workspace=workspace,
    )
    store.close()
    delegation_id = f"bestplan-{plan_id}"
    tracker = tmp_path / "async_delegations.json"
    tracker.write_text(json.dumps({
        "version": 1,
        "records": {delegation_id: {
            "status": "scheduled",
            "record": {
                "delegation_id": delegation_id, "status": "scheduled",
                "owner_pid": os.getpid(),
            },
        }},
    }), encoding="utf-8")
    restarted = BestplanStore(db_path=db_path)
    assert restarted.get_plan(plan_id)["state"] == PlanState.WAITING
    restarted.close()

    tracker.write_text(json.dumps({
        "version": 1,
        "records": {delegation_id: {
            "status": "completed",
            "record": {"delegation_id": delegation_id, "status": "completed"},
            "event": {"delegation_id": delegation_id, "status": "completed"},
        }},
    }), encoding="utf-8")
    terminal = BestplanStore(db_path=db_path)
    assert terminal.get_plan(plan_id)["state"] == PlanState.COMPLETED_UNVERIFIED
    assert json.loads(terminal.get_plan(plan_id)["evidence_json"])["delegation_id"] == delegation_id


def test_runtime_fingerprint_binds_every_execution_relevant_field(monkeypatch):
    from tools import delegate_tool

    monkeypatch.setattr(
        "agent.bestplan_sandbox.sandbox_backend_identity",
        lambda **_kwargs: {"backend": "sandbox", "policy_digest": "policy"},
    )
    task = {
        "route": "code_worker", "_bestplan_workspace": "/tmp/work",
        "_bestplan_leases": ["src/"], "_bestplan_read_only": False,
    }
    base = {
        "route": "code_worker", "provider": "p", "model": "m",
        "base_url": "https://user:secret@example.test/v1?token=hidden",
        "api_mode": "chat_completions", "toolsets": ["terminal", "file"],
        "command": "worker", "args": ["--safe"], "max_output_tokens": 1000,
        "request_overrides": {"temperature": 0, "api_key": "not-fingerprinted"},
    }
    first = delegate_tool._bestplan_runtime_identity(task, base)
    assert "secret" not in json.dumps(first["runtime_identity"])
    for key, value in {
        "route": "smart_reviewer", "provider": "q", "model": "n",
        "base_url": "https://other.test/v2", "api_mode": "responses",
        "toolsets": ["web"], "command": "other", "args": ["--other"],
        "max_output_tokens": 2000, "request_overrides": {"temperature": 1},
    }.items():
        changed = dict(base)
        changed[key] = value
        assert delegate_tool._bestplan_runtime_identity(task, changed)["runtime_fingerprint"] != first["runtime_fingerprint"]

    schemeless = dict(base, base_url="user:secret@example.test:8443/v1?token=hidden")
    endpoint = delegate_tool._bestplan_runtime_identity(task, schemeless)["runtime_identity"]["endpoint"]
    assert endpoint == "example.test:8443/v1"
    assert "secret" not in endpoint

    review_task = dict(task, _bestplan_read_only=True, _bestplan_leases=[])
    review = delegate_tool._bestplan_runtime_identity(review_task, base)
    assert review["bestplan_toolsets"] == ["read_only_files"]
    assert review["runtime_identity"]["toolsets"] == ["read_only_files"]


def test_detached_workspace_matches_clean_approved_tree_and_rejects_untracked_drift(
    tmp_path, monkeypatch,
):
    from tools import delegate_tool

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo = _git_repo(repo_path)
    (repo / "nested").mkdir()
    (repo / "nested" / "source.txt").write_text("approved", encoding="utf-8")
    subprocess.run(["git", "add", "nested/source.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "tracked source"], cwd=repo, check=True)
    home = tmp_path / "hermes-home"
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: home)

    detached = delegate_tool._bestplan_sandbox_workspace(str(repo), "parity")
    assert (detached / "tracked.txt").read_text(encoding="utf-8") == "base"
    assert (detached / "nested" / "source.txt").read_text(encoding="utf-8") == "approved"

    (repo / "untracked.txt").write_text("omitted", encoding="utf-8")
    with pytest.raises(ValueError, match="clean Git workspace"):
        delegate_tool._bestplan_sandbox_workspace(str(repo), "drift")


def test_unsupported_host_guards_are_wired_on_tui_and_gateway_production_paths():
    root = Path(__file__).resolve().parents[2]
    tui = (root / "tui_gateway" / "server.py").read_text(encoding="utf-8")
    gateway = (root / "gateway" / "run.py").read_text(encoding="utf-8")
    for source in (tui, gateway):
        assert "run_planning_only_bestplan_turn" in source


def test_old_host_context_keeps_new_hermes_strict_bestplan_disabled(monkeypatch):
    from tools import delegate_tool

    monkeypatch.setattr(
        delegate_tool, "_bestplan_sandbox_workspace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("capability rejection must happen before workspace mutation")
        ),
    )
    result = delegate_tool.dispatch_bestplan_tasks_async(
        tasks=[], parent_agent=object(), dispatch_id="d", plan_id="p",
        workspace="/tmp/work", resolved_runtimes=[],
    )
    assert result["status"] == "rejected"
    assert "capability/version" in result["error"]
