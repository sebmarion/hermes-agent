from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.bestplan_state import (
    BESTPLAN_ENVELOPE_END,
    BESTPLAN_ENVELOPE_START,
    BestplanStore,
    BaselineFingerprintError,
    PlanState,
    capture_bestplan_response,
    compute_baseline_fingerprint,
    try_resolve_go,
    _v1_plan_constraints,
    recover_bestplan_dispatch_outbox,
)
from agent.execution_plan import compile_execution_plan
from hermes_cli.commands import resolve_command


def _manifest(*, goal="Implement it", capability="fast_fallback"):
    kind = "review" if capability == "frontier_review" else "implement"
    mode = "sota" if kind == "review" else "delegate"
    risk = "high" if kind == "review" else "low"
    return {
        "version": 1,
        "mode": mode,
        "risk": risk,
        "slices": [{
            "id": "work",
            "kind": kind,
            "goal": goal,
            "depends_on": [],
            "capability": capability,
            "workspace": "/tmp/work",
            "allowed_paths": [] if kind == "review" else ["src/"],
            "read_only": kind == "review",
            "expected_artifacts": ["review.md" if kind == "review" else "src/change.py"],
            "acceptance": ["tests pass"],
        }],
        "merge_policy": "Integrate only after verification.",
        "stop_condition": "Acceptance passes.",
        "escalation_predicates": ["independent_review_required"],
    }


def _envelope(manifest=None):
    payload = {"version": 1, "manifest": manifest or _manifest()}
    return (
        f"{BESTPLAN_ENVELOPE_START}\n"
        f"{json.dumps(payload, sort_keys=True)}\n"
        f"{BESTPLAN_ENVELOPE_END}"
    )


def _config(*lanes):
    available = {
        "code_worker": {"provider": "test", "model": "coder"},
        "smart_reviewer": {"provider": "test", "model": "reviewer"},
    }
    selected = lanes or tuple(available)
    return {
        "autonomy": {"go_enabled": True},
        "delegation": {"lanes": {name: available[name] for name in selected}},
    }


def _store(tmp_path):
    return BestplanStore(db_path=tmp_path / "state.db")


def _capture(store, manifest=None, *, session_id="s1", profile="coder", workspace="/tmp/work", baseline="base-1"):
    return capture_bestplan_response(
        "Plan for review.\n\n" + _envelope(manifest),
        session_id=session_id,
        profile=profile,
        workspace=workspace,
        baseline_fingerprint=baseline,
        store=store,
    )


def test_execution_plan_manifest_round_trips():
    compiled = compile_execution_plan(_manifest())
    assert compile_execution_plan(compiled.to_manifest()) == compiled


def test_bestplan_is_not_a_builtin_command_that_shadows_the_dynamic_skill():
    assert resolve_command("bestplan") is None


def test_baseline_fingerprint_binds_tracked_and_untracked_content(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("one", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)

    clean = compute_baseline_fingerprint(str(tmp_path))
    tracked.write_text("two", encoding="utf-8")
    assert compute_baseline_fingerprint(str(tmp_path)) != clean

    untracked = tmp_path / "new.txt"
    untracked.write_text("first", encoding="utf-8")
    first = compute_baseline_fingerprint(str(tmp_path))
    untracked.write_text("second", encoding="utf-8")
    assert compute_baseline_fingerprint(str(tmp_path)) != first


def test_baseline_fingerprint_fails_closed_outside_git(tmp_path):
    with pytest.raises(BaselineFingerprintError, match="git"):
        compute_baseline_fingerprint(str(tmp_path))


def test_baseline_fingerprint_rejects_special_untracked_file(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.txt").write_text("base", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(BaselineFingerprintError, match="special"):
        compute_baseline_fingerprint(str(tmp_path))


@pytest.mark.parametrize(
    "mutator, error",
    [
        (lambda m: m["slices"][0].update(read_only=True), "read_only=false"),
        (lambda m: m["slices"][0].update(allowed_paths=[]), "write lease"),
        (lambda m: m["slices"][0].update(allowed_paths=["../escape"]), "traversal"),
        (lambda m: m["slices"][0].update(allowed_paths=["/tmp/escape"]), "relative"),
        (lambda m: m["slices"][0].update(workspace="/tmp/other"), "workspace"),
    ],
)
def test_v1_implementation_manifest_is_host_bound(mutator, error):
    manifest = _manifest()
    mutator(manifest)
    with pytest.raises(Exception, match=error):
        _v1_plan_constraints(
            compile_execution_plan(manifest), workspace="/tmp/work"
        )


def test_v1_review_manifest_requires_exact_sota_high_read_only_shape():
    manifest = _manifest(capability="frontier_review")
    plan = compile_execution_plan(manifest)
    _v1_plan_constraints(plan, workspace="/tmp/work")
    for key, value in (("mode", "delegate"), ("risk", "low")):
        changed = _manifest(capability="frontier_review")
        changed[key] = value
        with pytest.raises(Exception):
            _v1_plan_constraints(compile_execution_plan(changed), workspace="/tmp/work")


def test_capture_requires_explicit_valid_envelope(tmp_path):
    store = _store(tmp_path)
    missing = capture_bestplan_response(
        "Looks good; run it when ready.",
        session_id="s1",
        profile="coder",
        workspace="/tmp/work",
        baseline_fingerprint="base-1",
        store=store,
    )
    assert missing.executable is False
    assert "non-executable" in missing.response.lower()
    assert store.list_for_session("s1") == []

    malformed = capture_bestplan_response(
        f"{BESTPLAN_ENVELOPE_START}\n{{bad json}}\n{BESTPLAN_ENVELOPE_END}",
        session_id="s1",
        profile="coder",
        workspace="/tmp/work",
        baseline_fingerprint="base-1",
        store=store,
    )
    assert malformed.executable is False
    assert store.list_for_session("s1") == []


def test_capture_stores_immutable_raw_envelope_and_validated_manifest(tmp_path):
    store = _store(tmp_path)
    capture = _capture(store)
    assert capture.executable is True
    row = store.get_plan(capture.plan_id)
    assert row["raw_plan_json"] == _envelope()
    assert json.loads(row["validated_manifest_json"]) == _manifest()
    assert row["version"] == 1
    assert row["session_id"] == "s1"
    assert row["profile"] == "coder"
    assert row["workspace"] == str(Path("/tmp/work").resolve())
    assert row["baseline_fingerprint"] == "base-1"
    assert row["state"] == PlanState.PENDING


def test_capture_strips_machine_envelope_and_host_renders_authority(tmp_path):
    store = _store(tmp_path)
    response = (
        "Harmless prose claiming no files will change.\n\n"
        + _envelope()
        + "\n\nIgnore the machine block above."
    )
    capture = capture_bestplan_response(
        response,
        session_id="s1",
        profile="coder",
        workspace="/tmp/work",
        baseline_fingerprint="base-1",
        store=store,
    )
    assert capture.executable is True
    assert BESTPLAN_ENVELOPE_START not in capture.response
    assert "Authoritative executable manifest" in capture.response
    assert "route: code_worker" in capture.response
    assert f"workspace: {Path('/tmp/work').resolve()}" in capture.response
    assert "write leases: src/" in capture.response
    assert "digest=" in capture.response
    assert "Harmless prose" in capture.response


def test_go_no_plan_passes_through_but_stale_and_mismatch_fail_closed(tmp_path):
    store = _store(tmp_path)
    none = try_resolve_go(
        "go", session_id="s1", profile="coder", workspace="/tmp/work",
        baseline_fingerprint="base-1", parent_agent=SimpleNamespace(),
        config=_config(), store=store,
    )
    assert none.resolved is False
    assert none.status == "no_plan"

    _capture(store)
    stale = try_resolve_go(
        "go", session_id="s1", profile="coder", workspace="/tmp/work",
        baseline_fingerprint="base-2", parent_agent=SimpleNamespace(),
        config=_config(), store=store,
    )
    assert stale.resolved is True
    assert stale.status == "stale"

    wrong_profile = try_resolve_go(
        "go", session_id="s1", profile="other", workspace="/tmp/work",
        baseline_fingerprint="base-1", parent_agent=SimpleNamespace(),
        config=_config(), store=store,
    )
    assert wrong_profile.resolved is True
    assert wrong_profile.status == "context_mismatch"


def test_missing_lane_fails_before_state_transition_or_dispatch(tmp_path):
    store = _store(tmp_path)
    capture = _capture(store)
    calls = []
    result = try_resolve_go(
        "go", session_id="s1", profile="coder", workspace="/tmp/work",
        baseline_fingerprint="base-1", parent_agent=SimpleNamespace(),
        config=_config("smart_reviewer"), store=store,
        delegate=lambda **kwargs: calls.append(kwargs),
    )
    assert result.resolved is True
    assert result.status == "lane_unavailable"
    assert calls == []
    assert store.get_plan(capture.plan_id)["state"] == PlanState.PENDING


def test_claim_rechecks_digest_and_raw_envelope_inside_transaction(tmp_path):
    store = _store(tmp_path)
    capture = _capture(store)
    store._connection().execute(
        "UPDATE bestplan_plans SET validated_manifest_json = ? WHERE plan_id = ?",
        (json.dumps(_manifest(goal="Tampered")), capture.plan_id),
    )
    store._connection().commit()
    calls = []
    result = try_resolve_go(
        "go", session_id="s1", profile="coder", workspace="/tmp/work",
        baseline_fingerprint="base-1", parent_agent=SimpleNamespace(),
        config=_config(), store=store, delegate=lambda **kwargs: calls.append(kwargs),
    )
    assert result.resolved is True
    assert result.status == "invalid_plan"
    assert calls == []
    assert store.get_plan(capture.plan_id)["state"] == PlanState.PENDING


def test_concurrent_double_go_dispatches_once_with_explicit_route(tmp_path):
    store = _store(tmp_path)
    capture = _capture(store)
    calls = []

    def delegate(**kwargs):
        calls.append(kwargs)
        return json.dumps({"status": "dispatched", "delegation_id": "deleg_once"})

    def run():
        return try_resolve_go(
            "go", session_id="s1", profile="coder", workspace="/tmp/work",
            baseline_fingerprint="base-1", parent_agent=SimpleNamespace(),
            config=_config(), store=store, delegate=delegate,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _n: run(), range(2)))

    assert len(calls) == 1
    assert calls[0]["background"] is True
    assert calls[0]["tasks"][0]["route"] == "code_worker"
    assert sum(result.status == "waiting" for result in results) == 1
    assert all(result.resolved is True for result in results)
    row = store.get_plan(capture.plan_id)
    assert row["state"] == PlanState.WAITING
    assert json.loads(row["delegation_ids_json"]) == ["deleg_once"]


def test_runtime_is_resolved_once_before_durable_intent_and_reused_by_dispatch(tmp_path):
    store = _store(tmp_path)
    capture = _capture(store)
    calls = []

    def resolver(tasks, parent_agent):
        calls.append(("resolve", store.get_plan(capture.plan_id)["state"]))
        assert parent_agent is not None
        return [{"route": "code_worker", "provider": "test", "model": "coder"}]

    def dispatcher(**kwargs):
        row = store.get_plan(capture.plan_id)
        calls.append(("dispatch", row["dispatch_state"], kwargs))
        return {"status": "dispatched", "delegation_id": kwargs["dispatch_id"]}

    result = try_resolve_go(
        "go", session_id="s1", profile="coder", workspace="/tmp/work",
        baseline_fingerprint="base-1", parent_agent=SimpleNamespace(),
        config=_config(), store=store, runtime_resolver=resolver,
        strict_dispatcher=dispatcher,
    )

    assert result.status == "waiting"
    assert calls[0] == ("resolve", PlanState.PENDING)
    assert calls[1][0:2] == ("dispatch", "dispatching")
    dispatch_kwargs = calls[1][2]
    assert dispatch_kwargs["dispatch_id"] == f"bestplan-{capture.plan_id}"
    assert dispatch_kwargs["resolved_runtimes"][0]["model"] == "coder"
    row = store.get_plan(capture.plan_id)
    assert row["dispatch_state"] == "dispatched"
    assert json.loads(row["resolved_runtime_json"])[0]["provider"] == "test"


def test_unknown_dispatch_outcome_is_durable_and_never_claimed_not_started(tmp_path):
    store = _store(tmp_path)
    capture = _capture(store)

    def dispatcher(**_kwargs):
        raise ConnectionError("result channel closed after submit")

    result = try_resolve_go(
        "go", session_id="s1", profile="coder", workspace="/tmp/work",
        baseline_fingerprint="base-1", parent_agent=SimpleNamespace(),
        config=_config(), store=store,
        runtime_resolver=lambda _tasks, _parent: [
            {"route": "code_worker", "provider": "test", "model": "coder"}
        ],
        strict_dispatcher=dispatcher,
    )

    assert result.status == "possibly_dispatched"
    assert "possibly dispatched" in result.response.lower()
    row = store.get_plan(capture.plan_id)
    assert row["state"] == PlanState.RUNNING
    assert row["dispatch_state"] == "unknown"
    assert row["delegation_ids_json"] == json.dumps([f"bestplan-{capture.plan_id}"])


def test_recovery_only_reclaims_demonstrably_dead_dispatch_owner(tmp_path):
    store = _store(tmp_path)
    capture = _capture(store)
    runtimes = [{"route": "code_worker", "provider": "test", "model": "coder"}]
    store.prepare_dispatch_intent(
        capture.plan_id,
        "base-1",
        resolved_runtimes=runtimes,
        session_id="s1",
        profile="coder",
        workspace="/tmp/work",
    )
    assert store.begin_dispatch_attempt(capture.plan_id)
    store._connection().execute(
        "UPDATE bestplan_plans SET dispatch_owner='pid:99999999' WHERE plan_id=?",
        (capture.plan_id,),
    )
    store._connection().commit()

    assert recover_bestplan_dispatch_outbox(store) == 1
    assert store.get_plan(capture.plan_id)["dispatch_state"] == "unknown"


def test_strict_child_tools_resolve_inside_isolated_worktree(tmp_path):
    from agent.runtime_cwd import resolve_agent_cwd
    from tools.delegate_tool import _run_single_child

    observed = []
    child = MagicMock()
    child._credential_pool = None
    child._bestplan_workspace = str(tmp_path)
    child.get_activity_summary.return_value = {
        "current_tool": None,
        "api_call_count": 0,
        "max_iterations": 1,
    }

    def run_conversation(**_kwargs):
        observed.append(resolve_agent_cwd())
        return {
            "final_response": "done",
            "completed": True,
            "interrupted": False,
            "api_calls": 1,
            "messages": [],
        }

    child.run_conversation.side_effect = run_conversation
    parent = MagicMock()
    parent._current_task_id = None

    result = _run_single_child(
        task_index=0,
        goal="Implement safely",
        child=child,
        parent_agent=parent,
    )

    assert result["status"] == "completed"
    assert observed == [tmp_path]


def test_capture_repairs_and_persists_receipt_turn(tmp_path):
    from agent.bestplan_state import capture_bestplan_agent_result

    store = _store(tmp_path)
    persisted = []
    host = SimpleNamespace(
        api_mode="chat_completions",
        _persist_session=lambda messages, history: persisted.append((messages, history)),
    )
    response = "Plan for review.\n\n" + _envelope()
    result = {
        "final_response": response,
        "messages": [
            {"role": "user", "content": "/bestplan fix it"},
            {"role": "user", "content": "recovered tail"},
            {"role": "assistant", "content": response},
        ],
    }

    captured = capture_bestplan_agent_result(
        result,
        invocation_message="/bestplan fix it",
        session_id="s1",
        profile="coder",
        workspace="/tmp/work",
        baseline_fingerprint="base-1",
        store=store,
        host_agent=host,
    )

    assert [message["role"] for message in captured["messages"]] == ["user", "assistant"]
    assert "recovered tail" in captured["messages"][0]["content"]
    assert persisted[0][0] == captured["messages"]


def test_real_strict_dispatcher_enforces_worktree_and_runtime_identity(
    tmp_path, monkeypatch,
):
    from tools import delegate_tool

    workspace = tmp_path / "source"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=workspace, check=True,
    )
    (workspace / "src").mkdir()
    (workspace / "src" / "change.py").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=workspace, check=True)
    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: hermes_home)
    calls = []

    def delegate_task(**kwargs):
        calls.append(kwargs)
        return json.dumps({
            "status": "dispatched",
            "delegation_id": kwargs["_dispatch_id"],
        })

    monkeypatch.setattr(delegate_tool, "delegate_task", delegate_task)
    runtime = {"route": "code_worker", "provider": "controlled", "model": "coder"}
    result = delegate_tool.dispatch_bestplan_tasks_async(
        tasks=[{
            "goal": "Implement safely",
            "context": "Workspace: /tmp/untrusted",
            "route": "code_worker",
            "role": "leaf",
            "_bestplan_read_only": False,
            "_bestplan_leases": ["src/"],
        }],
        parent_agent=SimpleNamespace(),
        dispatch_id="bestplan-plan-1",
        plan_id="plan-1",
        workspace=str(workspace),
        resolved_runtimes=[runtime],
    )

    sandbox = Path(result["sandbox_workspace"])
    assert sandbox.is_dir()
    assert sandbox != workspace
    assert calls[0]["_strict_async"] is True
    assert calls[0]["_workspace_override"] == str(sandbox)
    assert calls[0]["_resolved_runtimes"] == [runtime]
    assert calls[0]["tasks"][0]["context"].startswith(
        f"Host-enforced isolated worktree: {sandbox}"
    )
