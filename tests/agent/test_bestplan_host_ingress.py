from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
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


def _capture(
    store,
    manifest=None,
    *,
    session_id="s1",
    profile="coder",
    workspace="/tmp/work",
    baseline="base-1",
    provisional=False,
):
    return capture_bestplan_response(
        "Plan for review.\n\n" + _envelope(manifest),
        session_id=session_id,
        profile=profile,
        workspace=workspace,
        baseline_fingerprint=baseline,
        store=store,
        provisional=provisional,
    )


def test_execution_plan_manifest_round_trips():
    compiled = compile_execution_plan(_manifest())
    assert compile_execution_plan(compiled.to_manifest()) == compiled


def test_bestplan_host_identity_keeps_dynamic_skill_and_blueprint_routes_distinct():
    bestplan = resolve_command("bestplan")

    assert bestplan is not None
    assert bestplan.busy_policy == "reject"
    assert bestplan.busy_handler is None
    assert bestplan.aliases == ()
    assert resolve_command("bp").name == "blueprint"


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

    with_paths = _manifest(capability="frontier_review")
    with_paths["slices"][0]["allowed_paths"] = ["src/"]
    with pytest.raises(Exception, match="allowed_paths"):
        _v1_plan_constraints(
            compile_execution_plan(with_paths), workspace="/tmp/work"
        )


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
    assert "advisory" not in missing.response.lower()
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
    assert BESTPLAN_ENVELOPE_START not in malformed.response
    assert BESTPLAN_ENVELOPE_END not in malformed.response
    assert store.list_for_session("s1") == []

    unterminated = capture_bestplan_response(
        f"Advisory prose.\n{BESTPLAN_ENVELOPE_START}\n{{bad json}}",
        session_id="s1",
        profile="coder",
        workspace="/tmp/work",
        baseline_fingerprint="base-1",
        store=store,
    )
    assert unterminated.executable is False
    assert "Advisory prose." not in unterminated.response
    assert BESTPLAN_ENVELOPE_START not in unterminated.response
    assert "{bad json}" not in unterminated.response


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


def test_provisional_capture_is_inert_until_transcript_commit(tmp_path):
    db_path = tmp_path / "state.db"
    store = BestplanStore(db_path=db_path)
    capture = _capture(store, provisional=True)
    assert capture.executable is True
    assert store.get_plan(capture.plan_id)["state"] == PlanState.PROVISIONAL
    assert store.list_for_session("s1", open_only=True) == []
    assert store.approve_plan(capture.plan_id) is False
    assert store.atomic_claim_approved(
        capture.plan_id,
        "base-1",
        session_id="s1",
        profile="coder",
        workspace="/tmp/work",
    ) is None
    assert store.prepare_dispatch_intent(
        capture.plan_id,
        "base-1",
        resolved_runtimes=[],
        session_id="s1",
        profile="coder",
        workspace="/tmp/work",
    ) is None
    resolved = try_resolve_go(
        "go",
        session_id="s1",
        profile="coder",
        workspace="/tmp/work",
        baseline_fingerprint="base-1",
        parent_agent=SimpleNamespace(),
        config=_config(),
        store=store,
    )
    assert resolved.resolved is False
    assert resolved.status == "no_plan"

    store.close()
    reopened = BestplanStore(db_path=db_path)
    assert reopened.get_plan(capture.plan_id)["state"] == PlanState.PROVISIONAL
    assert reopened.commit_provisional_plan(capture.plan_id) is True
    assert reopened.commit_provisional_plan(capture.plan_id) is False
    assert reopened.get_plan(capture.plan_id)["state"] == PlanState.PENDING
    assert [row["plan_id"] for row in reopened.list_for_session("s1")] == [
        capture.plan_id
    ]


@pytest.mark.parametrize(
    ("prior_state", "expected_state"),
    [
        (PlanState.PENDING, PlanState.REJECTED),
        (PlanState.APPROVED, PlanState.REJECTED),
        (PlanState.RUNNING, PlanState.RUNNING),
        (PlanState.WAITING, PlanState.WAITING),
    ],
)
def test_new_durable_capture_supersedes_only_unstarted_matching_plan(
    tmp_path, prior_state, expected_state,
):
    store = _store(tmp_path)
    prior = _capture(store)
    if prior_state == PlanState.APPROVED:
        assert store.approve_plan(prior.plan_id)
    elif prior_state in {PlanState.RUNNING, PlanState.WAITING}:
        claimed = store.prepare_dispatch_intent(
            prior.plan_id,
            "base-1",
            resolved_runtimes=[{
                "route": "code_worker",
                "provider": "test",
                "model": "coder",
            }],
            session_id="s1",
            profile="coder",
            workspace="/tmp/work",
        )
        assert claimed is not None
        if prior_state == PlanState.WAITING:
            assert store.record_dispatch(
                prior.plan_id,
                delegation_ids=[f"bestplan-{prior.plan_id}"],
            )

    replacement = _capture(store, provisional=True)
    assert store.commit_provisional_plan(replacement.plan_id)

    assert store.get_plan(prior.plan_id)["state"] == expected_state
    assert store.get_plan(replacement.plan_id)["state"] == PlanState.PENDING


def test_delayed_older_commit_does_not_supersede_newer_exact_session_plan(
    tmp_path, monkeypatch,
):
    import agent.bestplan_state as bestplan_state

    store = _store(tmp_path)
    monkeypatch.setattr(bestplan_state.time, "time", lambda: 100.0)
    older = _capture(store, provisional=True)
    monkeypatch.setattr(bestplan_state.time, "time", lambda: 200.0)
    newer = _capture(store, provisional=True)

    assert store.commit_provisional_plan(newer.plan_id)
    assert store.commit_provisional_plan(older.plan_id)

    assert store.get_plan(older.plan_id)["state"] == PlanState.PENDING
    assert store.get_plan(newer.plan_id)["state"] == PlanState.PENDING


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
    assert "BestPlan ready" in capture.response
    assert "No implementation or independent review has started" in capture.response
    assert BESTPLAN_ENVELOPE_START not in capture.response
    assert "Proposed action" in capture.response
    assert "Create or update `src/change.py`." in capture.response
    assert str(Path('/tmp/work').resolve()) not in capture.response
    assert "Authoritative executable manifest" not in capture.response
    assert "digest=" not in capture.response
    assert "Harmless prose" not in capture.response
    assert "Ignore the machine block above." not in capture.response


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
        return [{
            "route": "code_worker",
            "provider": "test",
            "model": "coder",
            "api_key": "top-level-secret",
            "base_url": (
                "https://user:pass@example.test/v1"
                "?token=url-secret#fragment"
            ),
            "endpoint_url": (
                "user:secret@example.test:8443/v1?token=schemeless-secret"
            ),
            "request_overrides": {
                "headers": {
                    "Authorization": "Bearer nested-secret",
                    "X-Api-Key": "nested-key-secret",
                },
                "temperature": 0.2,
            },
        }]

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
    assert set(dispatch_kwargs) == {
        "tasks",
        "parent_agent",
        "dispatch_id",
        "plan_id",
        "workspace",
        "resolved_runtimes",
    }
    assert dispatch_kwargs["dispatch_id"] == f"bestplan-{capture.plan_id}"
    assert dispatch_kwargs["resolved_runtimes"][0]["model"] == "coder"
    assert dispatch_kwargs["resolved_runtimes"][0]["api_key"] == "top-level-secret"
    assert (
        dispatch_kwargs["resolved_runtimes"][0]["request_overrides"]["headers"]
        ["Authorization"]
        == "Bearer nested-secret"
    )
    row = store.get_plan(capture.plan_id)
    assert row["dispatch_state"] == "scheduled"
    persisted_runtime = json.loads(row["resolved_runtime_json"])[0]
    assert persisted_runtime["provider"] == "test"
    assert persisted_runtime["base_url"] == "https://example.test/v1"
    assert persisted_runtime["endpoint_url"] == "example.test:8443/v1"
    assert persisted_runtime["request_overrides"] == {"temperature": 0.2}
    assert "secret" not in row["resolved_runtime_json"]


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


@pytest.mark.parametrize("post_admission", [False, True])
def test_reconcile_dead_running_tracker_marks_plan_lost_not_waiting(
    tmp_path, post_admission,
):
    store = _store(tmp_path)
    capture = _capture(store)
    store.prepare_dispatch_intent(
        capture.plan_id,
        "base-1",
        resolved_runtimes=[{
            "route": "code_worker", "provider": "test", "model": "coder",
        }],
        session_id="s1",
        profile="coder",
        workspace="/tmp/work",
    )
    delegation_id = f"bestplan-{capture.plan_id}"
    if post_admission:
        assert store.record_dispatch(
            capture.plan_id,
            delegation_ids=[delegation_id],
        )
        assert store.get_plan(capture.plan_id)["state"] == PlanState.WAITING
    tracker = tmp_path / "async_delegations.json"
    tracker.write_text(
        json.dumps({
            "version": 1,
            "records": {
                delegation_id: {
                    "delegation_id": delegation_id,
                    "status": "running",
                    "delivery_status": "running",
                    "record": {
                        "delegation_id": delegation_id,
                        "status": "running",
                        "owner_pid": 99_999_999,
                    },
                },
            },
        }),
        encoding="utf-8",
    )

    assert store.reconcile_async_tracker() == 1
    row = store.get_plan(capture.plan_id)
    assert row["state"] == PlanState.COMPLETED_UNVERIFIED
    assert row["dispatch_state"] == "terminal"
    evidence = json.loads(row["evidence_json"])
    assert evidence["status"] == "lost"


def test_reconcile_dead_scheduled_waiting_reopens_retryable_intent(tmp_path):
    store = _store(tmp_path)
    capture = _capture(store)
    store.prepare_dispatch_intent(
        capture.plan_id,
        "base-1",
        resolved_runtimes=[{
            "route": "code_worker", "provider": "test", "model": "coder",
        }],
        session_id="s1",
        profile="coder",
        workspace="/tmp/work",
    )
    delegation_id = f"bestplan-{capture.plan_id}"
    assert store.record_dispatch(capture.plan_id, delegation_ids=[delegation_id])
    (tmp_path / "async_delegations.json").write_text(
        json.dumps({
            "version": 1,
            "records": {
                delegation_id: {
                    "status": "scheduled",
                    "record": {
                        "delegation_id": delegation_id,
                        "status": "scheduled",
                        "owner_pid": 99_999_999,
                    },
                },
            },
        }),
        encoding="utf-8",
    )

    assert store.reconcile_async_tracker() == 1
    row = store.get_plan(capture.plan_id)
    assert row["state"] == PlanState.RUNNING
    assert row["dispatch_state"] == "intent"


def test_strict_child_tools_resolve_inside_isolated_worktree(tmp_path):
    from agent.runtime_cwd import resolve_agent_cwd
    from tools.delegate_tool import _DelegatedRequestContext, _run_single_child

    observed = []
    child = MagicMock()
    child._credential_pool = None
    child._bestplan_workspace = str(tmp_path)
    # Current-main execution/review children must prove successful tool use.
    # This test isolates task-local cwd propagation, so use the reasoning lane.
    child._delegate_mode = "reason"
    child._delegate_request_context = _DelegatedRequestContext(
        "Implement safely", None, str(tmp_path)
    )
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
        _persist_session=lambda messages, history, **_kwargs: (
            persisted.append((messages, history)) or True
        ),
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


def test_dynamic_skill_prose_cannot_persist_an_executable_plan(tmp_path):
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
        "messages": [{"role": "assistant", "content": response}],
    }

    captured = capture_bestplan_agent_result(
        result,
        invocation_message=(
            "[IMPORTANT: the user has invoked the bestplan skill]\n"
            "Treat this as a planning request."
        ),
        session_id="s1",
        profile="coder",
        workspace="/tmp/work",
        baseline_fingerprint="base-1",
        store=store,
        host_agent=host,
    )

    assert captured is result
    assert store.list_for_session("s1") == []
    assert persisted == []


def test_task5_dispatcher_does_not_precreate_or_reuse_legacy_plan_sandbox(
    tmp_path, monkeypatch,
):
    snapshot = _task5_snapshot(tmp_path)
    task = _task5_direct_task(snapshot.repo.workspace, "src/change.py")

    result, runtime, authority, execution, admission, legacy_launches = (
        _task5_direct_dispatch(
            tmp_path, monkeypatch, snapshot=snapshot, tasks=[task],
        )
    )

    assert result["status"] == "dispatched"
    assert execution == []
    assert len(admission) == 1
    assert legacy_launches == []
    assert authority.registered == 0
    assert not runtime.attempts_root.exists()

    combined = admission[0]["runner"]()
    assert len(execution) == 1
    assert len(combined["results"]) == 1
    assert execution[0]["attempts_root"] == runtime.attempts_root
    assert "sandbox_workspace" not in result


# ---------------------------------------------------------------------------
# Task 5 RED contracts: host-owned immutable inputs, protected admission, and
# append-only candidate-ready persistence.
# ---------------------------------------------------------------------------


def _task5_snapshot(
    tmp_path: Path,
    *,
    workspace_subdir: str = "",
    protected_paths: tuple[bytes, ...] = (),
    untracked_paths: tuple[bytes, ...] = (),
    special_paths: tuple[bytes, ...] = (),
):
    from agent.bestplan_source import (
        IndexEntry,
        IndexFlags,
        ProtectedManifest,
        ProtectedPath,
        RepoIdentity,
        SourceSnapshot,
    )

    root = (tmp_path / "source-repo").resolve()
    workspace = (root / workspace_subdir).resolve()
    common = root / ".git"
    workspace.mkdir(parents=True, exist_ok=True)
    common.mkdir()
    stat = common.stat()
    index_entries = tuple(
        IndexEntry(path, 0o100644, f"{index + 1:x}" * 40, 0)
        for index, path in enumerate(special_paths)
    )
    special_set = set(special_paths)
    index_flags = tuple(
        IndexFlags(
            entry.path,
            b"S ",
            b"",
            entry.path in special_set,
            False,
            False,
            False,
        )
        for entry in index_entries
    )
    worktree_entries = tuple(
        ProtectedPath(
            path=path,
            tracked=False,
            kind="regular",
            mode=0o100644,
            size=1,
            content_sha256=hashlib.sha256(b"x").hexdigest(),
            symlink_target=None,
            git_oid=None,
        )
        for path in untracked_paths
    )
    protected = ProtectedManifest(
        index_entries=index_entries,
        index_flags=index_flags,
        worktree_entries=worktree_entries,
        protected_paths=protected_paths,
        staged_diff_sha256="2" * 64,
        unstaged_diff_sha256="3" * 64,
        digest="4" * 64,
    )
    repo = RepoIdentity(
        workspace=str(workspace),
        workspace_raw=os.fsencode(workspace),
        worktree=str(root),
        worktree_raw=os.fsencode(root),
        git_dir=str(common),
        git_dir_raw=os.fsencode(common),
        common_dir=str(common),
        common_dir_raw=os.fsencode(common),
        common_dir_device=stat.st_dev,
        common_dir_inode=stat.st_ino,
        object_format="sha1",
        repository_id="task5-repository",
    )
    return SourceSnapshot(
        repo=repo,
        head_symbolic=True,
        head_ref=b"refs/heads/main",
        head_raw=b"ref: refs/heads/main\n",
        head_oid="5" * 40,
        tree_oid="6" * 40,
        protected_manifest=protected,
        capture_implementation_sha256="7" * 64,
        fingerprint="8" * 64,
    )


def _task5_controller(tmp_path: Path, snapshot):
    from agent.bestplan_contract import ControllerIdentity
    from agent.bestplan_sandbox import candidate_controller_artifact_sha256

    source = tmp_path / "retained-controller"
    (source / "agent").mkdir(parents=True)
    (source / "agent" / "bestplan_worker.py").write_text(
        "# retained controller\n", encoding="utf-8",
    )
    python = tmp_path / "retained-python" / "bin" / "python3.11"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"pinned python\n")
    python.chmod(0o755)
    runtime_lib = python.parent.parent / "lib" / "python3.11"
    (runtime_lib / "lib-dynload").mkdir(parents=True)
    (runtime_lib / "site-packages").mkdir()
    identity = ControllerIdentity(
        repository_id=snapshot.repo.repository_id,
        controller_id="controller-task5",
        release_oid="d" * 40,
        artifact_sha256=candidate_controller_artifact_sha256(source),
    )
    return source, python, identity


def _task5_host_runtime(tmp_path: Path, snapshot):
    from tools import delegate_tool
    from agent.bestplan_sandbox import pinned_candidate_runtime_paths

    controller_source, controller_python, controller = _task5_controller(
        tmp_path, snapshot,
    )
    runtime = delegate_tool.BestplanHostRuntime(
        controller=controller,
        controller_source=controller_source,
        controller_python=controller_python,
        runtime_read_paths=pinned_candidate_runtime_paths(controller_python),
        attempts_root=tmp_path / "attempts",
        policy_version=1,
        request_budget=4,
        token_budget=8192,
        max_iterations=8,
        max_output_tokens=1024,
        timeout_seconds=10,
        capability_ttl_seconds=60,
    )
    return runtime, controller


def _task5_command(identifier: str):
    from agent.bestplan_contract import BoundCommand, PinnedInput

    return BoundCommand(
        identifier=identifier,
        executable="/usr/bin/python3",
        executable_sha256="9" * 64,
        argv=("-m", "pytest", "-q"),
        logical_cwd="integration",
        env=(("PYTHONHASHSEED", "0"),),
        inputs=(PinnedInput("pyproject.toml", "a" * 64),),
        cache=(PinnedInput(".cache/pytest", "b" * 64),),
        timeout_seconds=60,
        network_allowlist=(),
    )


def _task5_enrollment(snapshot, controller, *, promotion_mode: str):
    from agent.bestplan_contract import (
        BlockingReview,
        EnrolledRepository,
        Enrollment,
        LiveTarget,
        Publication,
        RollbackTarget,
    )

    repository = EnrolledRepository.from_repo_identity(snapshot.repo)
    rollback = RollbackTarget(
        repository_id=snapshot.repo.repository_id,
        selector="/var/db/hermes/releases/current",
        service="com.example.hermes",
        command=_task5_command("rollback"),
    )
    live = LiveTarget(
        repository_id=snapshot.repo.repository_id,
        adapter="launchd",
        target_id="task5-live",
        service="com.example.hermes",
        activation=_task5_command("activate"),
        health=_task5_command("health"),
        canary=_task5_command("canary"),
        rollback=rollback,
    )
    return Enrollment(
        reference="task5-enrollment",
        enrollment_id="task5-enrollment-id",
        revision=1,
        epoch="task5-epoch",
        repository=repository,
        source_policy="head_only",
        capture_budget_seconds=30,
        local_ref="refs/heads/main",
        publication=Publication(
            repository_id=snapshot.repo.repository_id,
            remote_name="origin",
            push_url="https://example.test/hermes.git",
            remote_ref="refs/heads/main",
            observed_oid="c" * 40,
        ),
        commands=(_task5_command("focused-tests"),),
        review=BlockingReview(
            lane="smart_reviewer",
            command=_task5_command("review"),
            blocking_severities=("critical", "high"),
        ),
        live_targets=(live,),
        controller=controller,
        promotion_mode=promotion_mode,
    )


class _Task5Authority:
    def __init__(self, enrollment):
        self.enrollment = enrollment
        self.registered = []
        self.revoked = []

    def lookup_enrollment(self, _repo):
        return self.enrollment

    def register_model_attempt(self, *args, **kwargs):
        self.registered.append((args, kwargs))
        raise AssertionError("fake frozen candidates must not call the broker")

    def revoke_model_attempt(self, capability):
        self.revoked.append(capability)


def _task5_manifest(workspace: str, *, read_only: bool = False):
    slices = []
    labels = ("review",) if read_only else ("first", "second")
    for index, label in enumerate(labels):
        slices.append({
            "id": f"slice {label}/ß",
            "kind": "review" if read_only else "implement",
            "goal": f"Task 5 {label}",
            "depends_on": [],
            "capability": "frontier_review" if read_only else "fast_fallback",
            "workspace": workspace,
            "allowed_paths": [] if read_only else [f"src/{label}.py"],
            "read_only": read_only,
            "expected_artifacts": [
                "docs/existing-review.md" if read_only else f"src/{label}.py"
            ],
            "acceptance": [f"{label} accepted"],
        })
    return {
        "version": 1,
        "mode": "sota" if read_only else "delegate",
        "risk": "high" if read_only else "low",
        "slices": slices,
        "merge_policy": "host only",
        "stop_condition": "all candidates frozen",
        "escalation_predicates": ["independent review required"],
    }


def _task5_capture_v2(
    tmp_path: Path,
    monkeypatch,
    *,
    promotion_mode: str = "candidate_only",
    read_only: bool = False,
):
    snapshot = _task5_snapshot(tmp_path)
    runtime, controller = _task5_host_runtime(tmp_path, snapshot)
    authority = _Task5Authority(
        _task5_enrollment(snapshot, controller, promotion_mode=promotion_mode)
    )
    store = BestplanStore(db_path=tmp_path / "state.db")
    monkeypatch.setattr(
        "agent.bestplan_state.strong_source_capture_supported", lambda: True,
    )
    monkeypatch.setattr(
        "agent.bestplan_state.resolve_repo_identity", lambda _workspace: snapshot.repo,
    )
    monkeypatch.setattr(
        "agent.bestplan_state.capture_source_snapshot",
        lambda _repo, _deadline: snapshot,
    )
    manifest = _task5_manifest(snapshot.repo.workspace, read_only=read_only)
    capture = capture_bestplan_response(
        "Task 5 plan\n\n" + _envelope(manifest),
        session_id="task5-session",
        profile="coder",
        workspace=snapshot.repo.workspace,
        store=store,
        config={
            "bestplan_promotion": {
                "authority_endpoint": "unix:///task5-authority",
                "enrollment_ref": "task5-enrollment",
            }
        },
        authority_client=authority,
    )
    assert capture.executable is True
    assert store.get_plan(capture.plan_id)["execution_protocol"] == 2
    return store, capture, snapshot, runtime, authority


def _task5_frozen_candidate(spec, attempt_id: str, ordinal: int, controller):
    from agent import bestplan_candidates as candidate_module

    receipt = {
        "status": "completed",
        "summary": f"untrusted provider prose {ordinal}",
        "request_count": 1,
        "input_tokens": 5,
        "output_tokens": 3,
        "details": {"tool_results": ["untrusted nested prose"]},
    }
    canonical = json.dumps(
        receipt, sort_keys=True, separators=(",", ":"),
    ).encode("ascii")
    return candidate_module.FrozenCandidate(
        candidate_id=spec.candidate_id,
        slice_id=spec.slice_id,
        attempt_id=attempt_id,
        commit_oid=f"{ordinal + 1:x}" * 40,
        tree_oid=f"{ordinal + 9:x}" * 40,
        ref_name=(
            f"refs/hermes-bestplan/{spec.plan_id}/{spec.slice_id}/{attempt_id}"
        ),
        changed_paths=(
            ()
            if spec.read_only
            else (spec.expected_artifacts[0].encode("utf-8"),)
        ),
        raw_receipt=candidate_module._immutable_json_value(
            json.loads(canonical)
        ),
        raw_receipt_sha256=hashlib.sha256(canonical).hexdigest(),
        policy_digest=hashlib.sha256(attempt_id.encode("ascii")).hexdigest(),
        controller_id=controller.controller_id,
        controller_repository_id=controller.repository_id,
        controller_release_oid=controller.release_oid,
        controller_artifact_sha256=controller.artifact_sha256,
        admitted_requests=1,
        admitted_input_tokens=5,
        admitted_output_tokens=3,
    )


def _task5_thaw(value):
    if hasattr(value, "items"):
        return {str(key): _task5_thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_task5_thaw(item) for item in value]
    return value


def _task5_changed_paths_digest(paths: tuple[bytes, ...]) -> str:
    payload = json.dumps(
        [path.hex() for path in paths],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(
        b"hermes.bestplan.changed-paths.v1\0" + payload
    ).hexdigest()


def test_task5_host_runtime_is_frozen_nonsecret_and_policy_bounded(
    tmp_path, monkeypatch,
):
    from tools import delegate_tool

    snapshot = _task5_snapshot(tmp_path)
    runtime, _controller = _task5_host_runtime(tmp_path, snapshot)

    with pytest.raises(FrozenInstanceError):
        runtime.request_budget = 99
    projected = repr(runtime).casefold()
    assert "authority_client" not in projected
    assert "bestplanstore" not in projected
    assert "api_key" not in projected
    assert runtime.capability_ttl_seconds == 60

    with pytest.raises(ValueError, match="request budget"):
        replace(runtime, request_budget=0)
    with pytest.raises(ValueError, match="timeout"):
        replace(runtime, timeout_seconds=float("inf"))
    with pytest.raises(ValueError, match="capability ttl"):
        replace(runtime, capability_ttl_seconds=0)
    assert isinstance(runtime, delegate_tool.BestplanHostRuntime)

    digest = delegate_tool._bestplan_host_runtime_digest(runtime)
    assert digest == delegate_tool._bestplan_host_runtime_digest(replace(runtime))
    monkeypatch.setattr(time, "time", lambda: 9_000_000_000.0)
    assert digest == delegate_tool._bestplan_host_runtime_digest(runtime)

    controller_variants = (
        replace(runtime.controller, repository_id="task5-other-repository"),
        replace(runtime.controller, controller_id="controller-task5-next"),
        replace(runtime.controller, release_oid="e" * 40),
        replace(runtime.controller, artifact_sha256="f" * 64),
    )
    policy_variants = (
        replace(runtime, policy_version=2),
        replace(runtime, request_budget=5),
        replace(runtime, token_budget=8193),
        replace(runtime, max_iterations=9),
        replace(runtime, max_output_tokens=1025),
        replace(runtime, timeout_seconds=9),
        replace(runtime, capability_ttl_seconds=61),
    )
    assert all(
        delegate_tool._bestplan_host_runtime_digest(
            replace(runtime, controller=controller)
        ) != digest
        for controller in controller_variants
    )
    assert all(
        delegate_tool._bestplan_host_runtime_digest(variant) != digest
        for variant in policy_variants
    )


def test_task5_host_runtime_preserves_and_binds_a_symlinked_venv_launcher(
    tmp_path,
):
    from agent.bestplan_sandbox import pinned_candidate_runtime_paths
    from tools import delegate_tool

    snapshot = _task5_snapshot(tmp_path)
    controller_source, resolved_python, controller = _task5_controller(
        tmp_path, snapshot,
    )
    launcher = tmp_path / "retained-venv" / "bin" / "python"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(resolved_python)
    launcher_site = (
        launcher.parent.parent / "lib" / "python3.11" / "site-packages"
    )
    launcher_site.mkdir(parents=True)
    runtime = delegate_tool.BestplanHostRuntime(
        controller=controller,
        controller_source=controller_source,
        controller_python=launcher,
        runtime_read_paths=pinned_candidate_runtime_paths(launcher),
        attempts_root=tmp_path / "attempts-symlink",
        policy_version=1,
        request_budget=4,
        token_budget=8192,
        max_iterations=8,
        max_output_tokens=1024,
        timeout_seconds=10,
        capability_ttl_seconds=60,
    )
    body = delegate_tool._bestplan_host_runtime_body(runtime)

    assert runtime.controller_python == launcher.absolute()
    assert runtime.controller_python.is_symlink()
    assert runtime.controller_python != runtime.controller_python.resolve()
    assert body["controller_python"] == str(launcher.absolute())
    assert body["controller_python_resolved"] == str(resolved_python.resolve())


@pytest.mark.parametrize(
    ("read_only", "expected_toolsets"),
    ((False, ["file"]), (True, ["read_only_files"])),
)
def test_task5_v2_runtime_identity_is_process_free_and_not_legacy_sandbox_bound(
    monkeypatch, read_only, expected_toolsets,
):
    from tools import delegate_tool

    task = {
        "route": "code_worker",
        "_bestplan_workspace": "/tmp/task5-runtime-identity",
        "_bestplan_read_only": read_only,
        "_bestplan_leases": [] if read_only else ["src/result.py"],
    }
    monkeypatch.setattr(
        "agent.bestplan_sandbox.sandbox_backend_identity",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("V2 runtime identity used the legacy shared sandbox")
        ),
    )

    identity = delegate_tool._bestplan_runtime_identity(
        task,
        {
            "route": "code_worker",
            "provider": "authority-broker",
            "model": "model",
            "toolsets": ["terminal", "file"],
        },
        execution_protocol=2,
    )

    assert identity["bestplan_toolsets"] == expected_toolsets
    assert "terminal" not in identity["bestplan_toolsets"]
    assert identity["sandbox_policy_digest"] == ""


def test_task5_v2_resolved_runtime_drops_legacy_terminal_toolset(monkeypatch):
    from tools import delegate_tool

    task = {
        "route": "code_worker",
        "_bestplan_workspace": "/tmp/task5-runtime-identity",
        "_bestplan_read_only": False,
        "_bestplan_leases": ["src/result.py"],
    }
    monkeypatch.setattr(
        delegate_tool,
        "_load_config",
        lambda: {
            "lanes": {
                "code_worker": {
                    "provider": "test",
                    "model": "model",
                    "toolsets": ["terminal", "file"],
                }
            }
        },
    )
    monkeypatch.setattr(
        delegate_tool,
        "_resolve_delegation_credentials",
        lambda lane, _parent: dict(lane),
    )

    resolved = delegate_tool.resolve_bestplan_runtime_specs(
        [task], SimpleNamespace(), execution_protocol=2,
    )

    assert resolved[0]["toolsets"] == ["file"]
    assert resolved[0]["bestplan_toolsets"] == ["file"]
    assert "terminal" not in json.dumps(resolved, sort_keys=True)


def test_task5_go_handoff_carries_validated_immutable_context_and_frozen_policy(
    tmp_path, monkeypatch,
):
    store, capture, snapshot, runtime, authority = _task5_capture_v2(
        tmp_path, monkeypatch,
    )
    dispatched = []
    monkeypatch.setattr(
        "gateway.session_context.async_delivery_supported", lambda: True,
    )
    def strict_dispatcher(**kwargs):
        dispatched.append(kwargs)
        return {
            "status": "dispatched",
            "delegation_id": kwargs["dispatch_id"],
        }

    result = try_resolve_go(
        "go",
        session_id="task5-session",
        profile="coder",
        workspace=snapshot.repo.workspace,
        baseline_fingerprint=snapshot.fingerprint,
        parent_agent=SimpleNamespace(),
        config=_config(),
        store=store,
        runtime_resolver=lambda tasks, _parent: [
            {
                "route": task["route"],
                "provider": "authority-broker",
                "model": f"model-{index}",
                "toolsets": ["terminal", "file"],
                "runtime_fingerprint": hashlib.sha256(
                    f"runtime-{index}".encode("ascii")
                ).hexdigest(),
            }
            for index, task in enumerate(tasks)
        ],
        strict_dispatcher=strict_dispatcher,
        candidate_host_runtime=runtime,
        authority_client=authority,
    )

    assert result.status == "waiting"
    assert len(dispatched) == 1
    handoff = dispatched[0]
    row = store.get_plan(capture.plan_id)
    contract = json.loads(row["promotion_contract_json"])
    assert handoff["execution_protocol"] == 2
    assert handoff["source_snapshot"] == snapshot
    assert handoff["approval_digest"] == row["approval_digest"]
    assert handoff["promotion_contract"] == contract
    assert handoff["promotion_contract_digest"] == row[
        "promotion_contract_digest"
    ]
    assert handoff["promotion_mode"] == "candidate_only"
    assert handoff["candidate_host_runtime"] is runtime
    assert handoff["authority_client"] is authority
    assert handoff["state_db_path"] == store.state_db_path
    with pytest.raises(FrozenInstanceError):
        handoff["source_snapshot"].fingerprint = "0" * 64
    with pytest.raises(TypeError):
        handoff["promotion_contract"]["promotion_mode"] = "auto_live"
    with pytest.raises(TypeError):
        handoff["tasks"][0]["_bestplan_slice_id"] = "mutated"
    assert handoff["tasks"][0]["_bestplan_slice_id"] == "slice first/ß"
    assert handoff["tasks"][0]["_bestplan_acceptance"] == ["first accepted"]
    persisted = json.loads(row["resolved_runtime_json"])
    assert "terminal" not in json.dumps(
        {
            "execution": _task5_thaw(handoff["resolved_runtimes"]),
            "stored": persisted,
        },
        sort_keys=True,
    )
    assert all(
        runtime_item["toolsets"] == runtime_item["bestplan_toolsets"] == ["file"]
        for runtime_item in handoff["resolved_runtimes"]
    )
    for item in persisted:
        assert item["candidate_policy_version"] == 1
        assert item["candidate_request_budget"] == 4
        assert item["candidate_token_budget"] == 8192
        assert item["candidate_max_iterations"] == 8
        assert item["candidate_max_output_tokens"] == 1024
        assert item["candidate_timeout_seconds"] == 10
        assert item["candidate_capability_ttl_seconds"] == 60
        assert "candidate_expires_at" not in item
        assert len(item["candidate_host_runtime_digest"]) == 64


def test_task5_go_uses_the_existing_parent_bestplan_authority_fallback(
    tmp_path, monkeypatch,
):
    store, capture, snapshot, runtime, authority = _task5_capture_v2(
        tmp_path, monkeypatch,
    )
    dispatched = []
    monkeypatch.setattr(
        "gateway.session_context.async_delivery_supported", lambda: True,
    )

    result = try_resolve_go(
        "go",
        session_id="task5-session",
        profile="coder",
        workspace=snapshot.repo.workspace,
        baseline_fingerprint=snapshot.fingerprint,
        parent_agent=SimpleNamespace(
            candidate_host_runtime=runtime,
            bestplan_authority_client=authority,
        ),
        config=_config(),
        store=store,
        runtime_resolver=lambda tasks, _parent: [
            {"route": task["route"], "provider": "test", "model": "model"}
            for task in tasks
        ],
        strict_dispatcher=lambda **kwargs: dispatched.append(kwargs) or {
            "status": "dispatched",
            "delegation_id": kwargs["dispatch_id"],
        },
    )

    assert result.status == "waiting"
    assert len(dispatched) == 1
    assert dispatched[0]["authority_client"] is authority


def test_task5_store_locator_uses_the_active_session_database_path(tmp_path):
    from hermes_state import SessionDB

    session_db = SessionDB(tmp_path / "live-host-state.db")
    store = BestplanStore(session_db=session_db)
    try:
        assert store.state_db_path == session_db.db_path
    finally:
        session_db.close()


def test_task5_async_finalizer_uses_the_exact_handed_off_state_database(
    tmp_path, monkeypatch,
):
    from tools import async_delegation

    delegation_id = "task5-custom-state-locator"
    tracker_path = tmp_path / "async-records.json"
    state_db_path = (tmp_path / "custom-bestplan.sqlite3").resolve()
    opened = []
    marked = []

    class RecordingStore:
        def __init__(self, *, db_path):
            opened.append(Path(db_path))

        def mark_completed_unverified(self, plan_id, event):
            marked.append((plan_id, event["status"]))
            return True

        def close(self):
            return None

    monkeypatch.setattr("agent.bestplan_state.BestplanStore", RecordingStore)
    monkeypatch.setattr(
        async_delegation,
        "_persist_and_queue_terminal",
        lambda *_args, **_kwargs: True,
    )
    with async_delegation._records_lock:
        async_delegation._records[delegation_id] = {
            "delegation_id": delegation_id,
            "status": "running",
            "delivery_status": "running",
            "dispatched_at": time.time(),
            "origin_tracker_path": str(tracker_path),
            "bestplan_plan_id": "bp-task5-custom-state",
            "bestplan_state_db_path": str(state_db_path),
            "is_batch": True,
        }
    try:
        async_delegation._finalize_batch(
            delegation_id,
            {"results": [{"status": "frozen"}]},
            "completed",
        )
    finally:
        with async_delegation._records_lock:
            async_delegation._records.pop(delegation_id, None)

    assert opened == [state_db_path]
    assert opened[0] != tracker_path.parent / "state.db"
    assert marked == [("bp-task5-custom-state", "completed")]


@pytest.mark.parametrize(
    "bad_locator",
    ("relative-state.db", "/" + "x" * 5000),
)
def test_task5_async_state_database_locator_is_bounded_and_canonical(
    bad_locator,
):
    from tools import async_delegation

    started = []
    result = async_delegation.dispatch_async_delegation_batch(
        goals=["must not start"],
        context="Task 5 invalid state locator",
        toolsets=None,
        role="leaf",
        model="model",
        session_key="task5",
        runner=lambda: started.append(True) or {"results": []},
        delegation_id="task5-invalid-state-locator",
        bestplan_plan_id="bp-task5-invalid-state-locator",
        bestplan_state_db_path=bad_locator,
    )

    assert result == {
        "status": "rejected",
        "error": "bestplan_state_locator_invalid",
    }
    assert started == []


@pytest.mark.parametrize(
    "missing",
    ("candidate_host_runtime", "authority_client", "state_db_path"),
)
def test_task5_missing_host_context_fails_before_dispatch_intent(
    tmp_path, monkeypatch, missing,
):
    store, capture, snapshot, runtime, authority = _task5_capture_v2(
        tmp_path, monkeypatch,
    )
    monkeypatch.setattr(
        "gateway.session_context.async_delivery_supported", lambda: True,
    )
    dispatched = []
    values = {
        "candidate_host_runtime": runtime,
        "authority_client": authority,
        "state_db_path": store.state_db_path,
    }
    values[missing] = None
    if missing == "state_db_path":
        # The durable store locator is derived from the exact caller-owned
        # store, not supplied as a second public argument.
        store._db_path = None

    result = try_resolve_go(
        "go",
        session_id="task5-session",
        profile="coder",
        workspace=snapshot.repo.workspace,
        baseline_fingerprint=snapshot.fingerprint,
        parent_agent=SimpleNamespace(),
        config=_config(),
        store=store,
        runtime_resolver=lambda tasks, _parent: [
            {"route": task["route"], "provider": "test", "model": "model"}
            for task in tasks
        ],
        strict_dispatcher=lambda **kwargs: dispatched.append(kwargs),
        candidate_host_runtime=values["candidate_host_runtime"],
        authority_client=values["authority_client"],
    )

    assert result.status == "candidate_runtime_unavailable"
    assert dispatched == []
    row = store.get_plan(capture.plan_id)
    assert row["state"] == PlanState.PENDING
    assert row["dispatch_state"] is None


def test_task5_default_protocol1_go_requires_full_host_context(
    tmp_path, monkeypatch,
):
    store = _store(tmp_path)
    capture = _capture(store)
    dispatched = []
    monkeypatch.setattr(
        "gateway.session_context.async_delivery_supported", lambda: True,
    )
    monkeypatch.setattr(
        "tools.delegate_tool.dispatch_bestplan_tasks_async",
        lambda **kwargs: dispatched.append(kwargs),
    )

    result = try_resolve_go(
        "go",
        session_id="s1",
        profile="coder",
        workspace="/tmp/work",
        baseline_fingerprint="base-1",
        parent_agent=SimpleNamespace(),
        config=_config(),
        store=store,
    )

    assert result.status == "candidate_runtime_unavailable"
    assert dispatched == []
    row = store.get_plan(capture.plan_id)
    assert row["state"] == PlanState.PENDING
    assert row["dispatch_state"] is None


def test_task5_v2_runtime_controller_mismatch_fails_before_dispatch_intent(
    tmp_path, monkeypatch,
):
    store, capture, snapshot, runtime, authority = _task5_capture_v2(
        tmp_path, monkeypatch,
    )
    mismatched = replace(
        runtime,
        controller=replace(
            runtime.controller,
            artifact_sha256=(
                "f" * 64
                if runtime.controller.artifact_sha256 != "f" * 64
                else "e" * 64
            ),
        ),
    )
    dispatched = []
    monkeypatch.setattr(
        "gateway.session_context.async_delivery_supported", lambda: True,
    )

    result = try_resolve_go(
        "go",
        session_id="task5-session",
        profile="coder",
        workspace=snapshot.repo.workspace,
        baseline_fingerprint=snapshot.fingerprint,
        parent_agent=SimpleNamespace(),
        config=_config(),
        store=store,
        runtime_resolver=lambda tasks, _parent: [
            {"route": task["route"], "provider": "test", "model": "model"}
            for task in tasks
        ],
        strict_dispatcher=lambda **kwargs: dispatched.append(kwargs),
        candidate_host_runtime=mismatched,
        authority_client=authority,
    )

    assert result.status == "candidate_runtime_unavailable"
    assert dispatched == []
    row = store.get_plan(capture.plan_id)
    assert row["state"] == PlanState.PENDING
    assert row["dispatch_state"] is None


@pytest.mark.parametrize("mutation", ("missing", "extra"))
def test_task5_v2_runtime_dependency_mismatch_fails_before_dispatch_intent(
    tmp_path, monkeypatch, mutation,
):
    store, capture, snapshot, runtime, authority = _task5_capture_v2(
        tmp_path, monkeypatch,
    )
    runtime_paths = runtime.runtime_read_paths
    if mutation == "missing":
        mutated_paths = runtime_paths[:-1]
    else:
        unrelated = tmp_path / "unrelated-runtime"
        unrelated.mkdir()
        mutated_paths = (*runtime_paths, unrelated)
    mismatched = replace(runtime, runtime_read_paths=mutated_paths)
    dispatched = []
    monkeypatch.setattr(
        "gateway.session_context.async_delivery_supported", lambda: True,
    )

    result = try_resolve_go(
        "go",
        session_id="task5-session",
        profile="coder",
        workspace=snapshot.repo.workspace,
        baseline_fingerprint=snapshot.fingerprint,
        parent_agent=SimpleNamespace(),
        config=_config(),
        store=store,
        runtime_resolver=lambda tasks, _parent: [
            {"route": task["route"], "provider": "test", "model": "model"}
            for task in tasks
        ],
        strict_dispatcher=lambda **kwargs: dispatched.append(kwargs),
        candidate_host_runtime=mismatched,
        authority_client=authority,
    )

    assert result.status == "candidate_runtime_unavailable"
    assert dispatched == []
    row = store.get_plan(capture.plan_id)
    assert row["state"] == PlanState.PENDING
    assert row["dispatch_state"] is None
    assert not runtime.attempts_root.exists()


def test_task5_protocol1_missing_source_snapshot_has_zero_dispatch_side_effects(
    tmp_path, monkeypatch,
):
    from tools import delegate_tool

    snapshot = _task5_snapshot(tmp_path)
    runtime, _controller = _task5_host_runtime(tmp_path, snapshot)
    task = _task5_direct_task(snapshot.repo.workspace, "src/result.py")
    admissions = []
    executions = []
    legacy_launches = []
    authority = _Task5NoBrokerAuthority()
    monkeypatch.setattr(
        "agent.bestplan_candidates.run_and_freeze_candidate",
        lambda **kwargs: executions.append(kwargs),
    )
    monkeypatch.setattr(
        "tools.async_delegation.dispatch_async_delegation_batch",
        lambda **kwargs: admissions.append(kwargs),
    )
    monkeypatch.setattr(
        "agent.bestplan_sandbox.create_bestplan_sandbox_launch",
        lambda **kwargs: legacy_launches.append(kwargs),
    )

    result = delegate_tool.dispatch_bestplan_tasks_async(
        tasks=[task],
        parent_agent=SimpleNamespace(),
        dispatch_id="bestplan-task5-missing-snapshot",
        plan_id="bp-task5-missing-snapshot",
        workspace=snapshot.repo.workspace,
        resolved_runtimes=[{
            "route": "code_worker",
            "provider": "authority-broker",
            "model": "model",
            "runtime_fingerprint": "a" * 64,
        }],
        execution_protocol=1,
        source_snapshot=None,
        approval_digest="b" * 64,
        promotion_contract=None,
        promotion_mode=None,
        candidate_host_runtime=runtime,
        authority_client=authority,
        state_db_path=tmp_path / "state.db",
    )

    assert result == {"status": "rejected", "error": "source_snapshot_required"}
    assert admissions == executions == legacy_launches == []
    assert authority.registered == 0
    assert authority.revoked == []
    assert not runtime.attempts_root.exists()


class _Task5NoBrokerAuthority:
    def __init__(self):
        self.registered = 0
        self.revoked = []

    def register_model_attempt(self, *_args, **_kwargs):
        self.registered += 1
        raise AssertionError("protected admission reached model capability issuance")

    def revoke_model_attempt(self, capability):
        self.revoked.append(capability)


def _task5_direct_dispatch(
    tmp_path,
    monkeypatch,
    *,
    snapshot,
    tasks,
    async_result=None,
):
    from tools import delegate_tool

    runtime, _controller = _task5_host_runtime(tmp_path, snapshot)
    authority = _Task5NoBrokerAuthority()
    execution = []
    admission = []

    def candidate_runner(**kwargs):
        execution.append(kwargs)
        return _task5_frozen_candidate(
            kwargs["spec"],
            kwargs["attempt_id"],
            len(execution) - 1,
            runtime.controller,
        )

    def async_dispatch(**kwargs):
        admission.append(kwargs)
        return async_result or {
            "status": "dispatched",
            "delegation_id": kwargs["delegation_id"],
        }

    monkeypatch.setattr(
        "agent.bestplan_candidates.run_and_freeze_candidate", candidate_runner,
    )
    monkeypatch.setattr(
        "tools.async_delegation.dispatch_async_delegation_batch", async_dispatch,
    )
    monkeypatch.setattr(
        "gateway.session_context.get_delivery_context_identity",
        lambda: {
            "capability_version": 1,
            "session_key": "task5-session",
            "session_id": "task5-session",
            "ui_session_id": "task5-session",
            "profile": "coder",
            "tracker_path": str(tmp_path / "async.json"),
        },
    )
    launches = []
    monkeypatch.setattr(
        "agent.bestplan_sandbox.create_bestplan_sandbox_launch",
        lambda **kwargs: launches.append(kwargs) or (_ for _ in ()).throw(
            AssertionError("legacy launch profile was created")
        ),
    )
    result = delegate_tool.dispatch_bestplan_tasks_async(
        tasks=tasks,
        parent_agent=SimpleNamespace(),
        dispatch_id="bestplan-task5-direct",
        plan_id="bp-task5-direct",
        workspace=snapshot.repo.workspace,
        resolved_runtimes=[
            {
                "route": task["route"],
                "provider": "authority-broker",
                "model": f"model-{index}",
                "runtime_fingerprint": hashlib.sha256(
                    f"runtime-{index}".encode("ascii")
                ).hexdigest(),
            }
            for index, task in enumerate(tasks)
        ],
        execution_protocol=1,
        source_snapshot=snapshot,
        approval_digest="a" * 64,
        promotion_contract=None,
        promotion_mode=None,
        candidate_host_runtime=runtime,
        authority_client=authority,
        state_db_path=tmp_path / "state.db",
    )
    return result, runtime, authority, execution, admission, launches


def _task5_direct_task(workspace: str, lease: str, *, index: int = 0):
    return {
        "goal": f"direct slice {index}",
        "context": f"Workspace: {workspace}",
        "route": "code_worker",
        "role": "leaf",
        "_bestplan_slice_id": f"raw slice {index}/ß",
        "_bestplan_manifest_index": index,
        "_bestplan_read_only": False,
        "_bestplan_leases": [lease],
        "_bestplan_workspace": workspace,
        "_bestplan_expected_artifacts": [lease],
        "_bestplan_acceptance": ["host frozen"],
    }


@pytest.mark.parametrize(
    ("protected", "lease"),
    (
        ("Src/Café.py", "Src/Café.py"),
        ("Src/Café.py", "Src"),
        ("Src/Café.py", "Src/Café.py/generated"),
        ("Src/Café.py", "src/cafe\u0301.py"),
    ),
)
def test_task5_protected_lease_overlap_rejected_before_any_execution(
    tmp_path, monkeypatch, protected, lease,
):
    raw = os.fsencode(f"pkg/{protected}")
    snapshot = _task5_snapshot(
        tmp_path,
        workspace_subdir="pkg",
        protected_paths=(raw,),
        untracked_paths=(raw,),
    )
    task = _task5_direct_task(snapshot.repo.workspace, lease=lease)

    result, runtime, authority, execution, admission, launches = (
        _task5_direct_dispatch(
            tmp_path, monkeypatch, snapshot=snapshot, tasks=[task],
        )
    )

    assert result["status"] == "rejected"
    assert result["error"] == "protected_path_overlap"
    assert execution == []
    assert admission == []
    assert launches == []
    assert authority.registered == 0
    assert not runtime.attempts_root.exists()


@pytest.mark.parametrize(
    ("first_lease", "second_lease"),
    (
        ("src/Result.py", "SRC/result.py"),
        ("src/Caf\u00e9.py", "src/cafe\u0301.py"),
    ),
)
def test_task5_cross_slice_casefold_or_nfc_lease_alias_rejected_pre_admission(
    tmp_path, monkeypatch, first_lease, second_lease,
):
    snapshot = _task5_snapshot(tmp_path)
    tasks = [
        _task5_direct_task(
            snapshot.repo.workspace, first_lease, index=0,
        ),
        _task5_direct_task(
            snapshot.repo.workspace, second_lease, index=1,
        ),
    ]

    result, runtime, authority, execution, admission, launches = (
        _task5_direct_dispatch(
            tmp_path, monkeypatch, snapshot=snapshot, tasks=tasks,
        )
    )

    assert result == {"status": "rejected", "error": "lease_alias"}
    assert execution == admission == launches == []
    assert authority.registered == 0
    assert authority.revoked == []
    assert not runtime.attempts_root.exists()


def test_task5_invalid_later_slice_preflight_prevents_first_slice_side_effects(
    tmp_path, monkeypatch,
):
    protected = b"pkg/protected.py"
    snapshot = _task5_snapshot(
        tmp_path,
        workspace_subdir="pkg",
        protected_paths=(protected,),
        untracked_paths=(protected,),
    )
    tasks = [
        _task5_direct_task(snapshot.repo.workspace, "safe/first.py", index=0),
        _task5_direct_task(snapshot.repo.workspace, "protected.py", index=1),
    ]

    result, runtime, authority, execution, admission, launches = (
        _task5_direct_dispatch(
            tmp_path, monkeypatch, snapshot=snapshot, tasks=tasks,
        )
    )

    assert result["status"] == "rejected"
    assert execution == admission == launches == []
    assert authority.registered == 0
    assert not runtime.attempts_root.exists()


@pytest.mark.parametrize("omitted_kind", ("untracked", "special"))
def test_task5_inconsistent_protected_projection_rejected_before_execution(
    tmp_path, monkeypatch, omitted_kind,
):
    raw = b"pkg/must-protect.txt"
    snapshot = _task5_snapshot(
        tmp_path,
        workspace_subdir="pkg",
        protected_paths=(),
        untracked_paths=(raw,) if omitted_kind == "untracked" else (),
        special_paths=(raw,) if omitted_kind == "special" else (),
    )
    task = _task5_direct_task(snapshot.repo.workspace, "safe/result.py")

    result, runtime, authority, execution, admission, launches = (
        _task5_direct_dispatch(
            tmp_path, monkeypatch, snapshot=snapshot, tasks=[task],
        )
    )

    assert result["status"] == "rejected"
    assert result["error"] == "protected_manifest_inconsistent"
    assert execution == admission == launches == []
    assert authority.registered == 0
    assert not runtime.attempts_root.exists()


def test_task5_async_admission_rejection_creates_no_attempt_profile_or_capability(
    tmp_path, monkeypatch,
):
    snapshot = _task5_snapshot(tmp_path)
    task = _task5_direct_task(snapshot.repo.workspace, "safe/result.py")

    result, runtime, authority, execution, admission, launches = (
        _task5_direct_dispatch(
            tmp_path,
            monkeypatch,
            snapshot=snapshot,
            tasks=[task],
            async_result={"status": "rejected", "error": "capacity"},
        )
    )

    assert result == {"status": "rejected", "error": "capacity"}
    assert len(admission) == 1
    assert execution == []
    assert launches == []
    assert authority.registered == 0
    assert not runtime.attempts_root.exists()


def test_task5_subdirectory_paths_and_original_slice_ids_are_host_bound(
    tmp_path, monkeypatch,
):
    snapshot = _task5_snapshot(tmp_path, workspace_subdir="pkg")
    task = _task5_direct_task(snapshot.repo.workspace, "src/new.py")

    result, _runtime, _authority, execution, admission, launches = (
        _task5_direct_dispatch(
            tmp_path, monkeypatch, snapshot=snapshot, tasks=[task],
        )
    )
    combined = admission[0]["runner"]()

    assert result["status"] == "dispatched"
    assert launches == []
    assert len(execution) == 1
    spec = execution[0]["spec"]
    assert spec.allowed_paths == ("pkg/src/new.py",)
    assert spec.expected_artifacts == ("pkg/src/new.py",)
    assert spec.toolsets == ("file",)
    assert "host frozen" in spec.goal
    assert "pkg/src/new.py" in spec.goal
    assert spec.goal.endswith("direct slice 0")
    assert spec.slice_id.isascii()
    assert spec.candidate_id.isascii()
    assert " " not in spec.slice_id and "/" not in spec.slice_id
    assert combined["results"][0]["manifest_slice_id"] == "raw slice 0/ß"
    assert combined["results"][0]["slice_id"] == spec.slice_id
    assert "untrusted provider prose" not in json.dumps(combined, sort_keys=True)


def _task5_run_v2_batch(
    tmp_path,
    monkeypatch,
    *,
    promotion_mode: str,
    read_only: bool = False,
    run_during_dispatch: bool = False,
):
    from agent import bestplan_proof

    store, capture, snapshot, runtime, authority = _task5_capture_v2(
        tmp_path,
        monkeypatch,
        promotion_mode=promotion_mode,
        read_only=read_only,
    )
    admitted = {}
    started = []
    frozen_candidates = []
    persistence_order = []
    raw_candidate_receipts = []
    persistence_stores = []
    inline_combined = []

    def candidate_runner(**kwargs):
        started.append(kwargs)
        frozen = _task5_frozen_candidate(
            kwargs["spec"],
            kwargs["attempt_id"],
            len(started) - 1,
            runtime.controller,
        )
        frozen_candidates.append(frozen)
        return frozen

    def async_dispatch(**kwargs):
        admitted.update(kwargs)
        if run_during_dispatch:
            combined = kwargs["runner"]()
            inline_combined.append(combined)
            assert store.mark_completed_unverified(capture.plan_id, combined)
        return {
            "status": "dispatched",
            "delegation_id": kwargs["delegation_id"],
        }

    original_record = bestplan_proof.ProofLedger.record_candidate
    original_append = bestplan_proof.ProofLedger.append_event

    def record_candidate(self, **kwargs):
        persistence_order.append(("candidate", kwargs["candidate_id"]))
        raw_candidate_receipts.append(_task5_thaw(kwargs))
        persistence_stores.append(self.store)
        return original_record(self, **kwargs)

    def append_event(self, **kwargs):
        persistence_order.append(("event", kwargs["phase"]))
        persistence_stores.append(self.store)
        return original_append(self, **kwargs)

    monkeypatch.setattr(
        "agent.bestplan_candidates.run_and_freeze_candidate", candidate_runner,
    )
    monkeypatch.setattr(
        "tools.async_delegation.dispatch_async_delegation_batch", async_dispatch,
    )
    monkeypatch.setattr(
        bestplan_proof.ProofLedger, "record_candidate", record_candidate,
    )
    monkeypatch.setattr(bestplan_proof.ProofLedger, "append_event", append_event)
    monkeypatch.setattr(
        "gateway.session_context.async_delivery_supported", lambda: True,
    )
    monkeypatch.setattr(
        "gateway.session_context.get_delivery_context_identity",
        lambda: {
            "capability_version": 1,
            "session_key": "task5-session",
            "session_id": "task5-session",
            "ui_session_id": "task5-session",
            "profile": "coder",
            "tracker_path": str(tmp_path / "async.json"),
        },
    )
    result = try_resolve_go(
        "go",
        session_id="task5-session",
        profile="coder",
        workspace=snapshot.repo.workspace,
        baseline_fingerprint=snapshot.fingerprint,
        parent_agent=SimpleNamespace(),
        config=_config(),
        store=store,
        runtime_resolver=lambda tasks, _parent: [
            {
                "route": task["route"],
                "provider": "authority-broker",
                "model": f"model-{index}",
                "runtime_fingerprint": hashlib.sha256(
                    f"runtime-{index}".encode("ascii")
                ).hexdigest(),
            }
            for index, task in enumerate(tasks)
        ],
        candidate_host_runtime=runtime,
        authority_client=authority,
    )
    assert result.status == "waiting"
    if run_during_dispatch:
        assert len(inline_combined) == 1
        combined = inline_combined[0]
    else:
        # Candidate work is a runner-owned side effect and cannot start while
        # async admission establishes its durable accepted/running checkpoint.
        assert started == []
        assert store.get_plan(capture.plan_id)["dispatch_state"] == "scheduled"
        combined = admitted["runner"]()
    return {
        "store": store,
        "capture": capture,
        "snapshot": snapshot,
        "runtime": runtime,
        "authority": authority,
        "admitted": admitted,
        "started": started,
        "frozen_candidates": frozen_candidates,
        "persistence_order": persistence_order,
        "raw_candidate_receipts": raw_candidate_receipts,
        "persistence_stores": persistence_stores,
        "combined": combined,
    }


def _task5_advance_to_queued(store, plan_id):
    from agent.bestplan_proof import ProofLedger

    ledger = ProofLedger(store)
    candidate_ready = [
        event for event in ledger.read_events(plan_id)
        if event.stream == "authority"
    ][-1]
    return ledger.append_event(
        plan_id=plan_id,
        authority_epoch=candidate_ready.authority_epoch,
        operation_id="00000000-0000-0000-0000-000000000012",
        expected_epoch=candidate_ready.authority_epoch,
        expected_seq=candidate_ready.event_seq,
        expected_hash=candidate_ready.event_hash,
        kind="queued",
        phase="queued",
        projected_state=PlanState.RUNNING,
        integration_oid=None,
        artifact_digest=None,
        origin="promoter",
        raw_output={"status": "queued"},
        output_source="promoter",
        contract_digest=store.get_plan(plan_id)["promotion_contract_digest"],
    )


def _task5_write_async_tracker(
    tmp_path, delegation_id, status, *, owner_pid=99_999_999,
):
    (tmp_path / "async_delegations.json").write_text(
        json.dumps({
            "version": 1,
            "records": {
                delegation_id: {
                    "delegation_id": delegation_id,
                    "status": status,
                    "record": {
                        "delegation_id": delegation_id,
                        "status": status,
                        "owner_pid": owner_pid,
                    },
                },
            },
        }),
        encoding="utf-8",
    )


_TASK5_AUTHORITY_PROJECTION_FIELDS = (
    "state",
    "current_phase",
    "candidate_set_digest",
    "integration_oid",
    "artifact_digest",
    "proof_authority_epoch",
    "proof_event_seq",
    "proof_event_hash",
    "verification_receipt_json",
    "verification_receipt_digest",
    "tests_verified_at",
    "review_verified_at",
    "remote_verified_at",
    "live_verified_at",
    "verified_at",
    "completed_at",
    )


@pytest.mark.parametrize("tracker_status", ("scheduled", "running"))
def test_task5_reconcile_stale_active_tracker_cannot_regress_captured_terminal(
    tmp_path, monkeypatch, tracker_status,
):
    store, capture, snapshot, _runtime, _authority = _task5_capture_v2(
        tmp_path,
        monkeypatch,
        promotion_mode="candidate_only",
        read_only=True,
    )
    claimed = store.prepare_dispatch_intent(
        capture.plan_id,
        snapshot.fingerprint,
        resolved_runtimes=[{
            "route": "smart_reviewer",
            "provider": "test",
            "model": "reviewer",
        }],
        session_id="task5-session",
        profile="coder",
        workspace=snapshot.repo.workspace,
    )
    assert claimed is not None
    assert store.begin_dispatch_attempt(capture.plan_id)
    assert store.mark_completed_unverified(
        capture.plan_id,
        {"status": "error", "results": []},
    )
    before = store.get_plan(capture.plan_id)
    assert before["current_phase"] == "captured"
    assert before["dispatch_state"] == "terminal"
    assert before["dispatch_owner"] is None
    assert before["error"] == "recapture_required"

    _task5_write_async_tracker(
        tmp_path,
        f"bestplan-{capture.plan_id}",
        tracker_status,
        owner_pid=os.getpid(),
    )
    store.close()

    reopened = BestplanStore(db_path=tmp_path / "state.db")
    after = reopened.get_plan(capture.plan_id)

    fields = _TASK5_AUTHORITY_PROJECTION_FIELDS + (
        "error", "dispatch_state", "dispatch_owner",
    )
    assert tuple(after[name] for name in fields) == tuple(
        before[name] for name in fields
    )


@pytest.mark.parametrize("authority_phase", ("candidate_ready", "queued"))
def test_task5_reconcile_terminal_tracker_is_phase_aware_after_reopen(
    tmp_path, monkeypatch, authority_phase,
):
    from agent import bestplan_proof
    from agent.bestplan_proof import ProofLedger

    context = _task5_run_v2_batch(
        tmp_path, monkeypatch, promotion_mode="candidate_only",
    )
    store = context["store"]
    plan_id = context["capture"].plan_id
    if authority_phase == "queued":
        _task5_advance_to_queued(store, plan_id)
    before = store.get_plan(plan_id)
    delegation_id = f"bestplan-{plan_id}"
    _task5_write_async_tracker(tmp_path, delegation_id, "completed")
    store.close()

    reopened = BestplanStore(db_path=tmp_path / "state.db")
    after = reopened.get_plan(plan_id)

    assert tuple(after[name] for name in _TASK5_AUTHORITY_PROJECTION_FIELDS) == tuple(
        before[name] for name in _TASK5_AUTHORITY_PROJECTION_FIELDS
    )
    if authority_phase == "candidate_ready":
        assert after["dispatch_state"] == "terminal"
        assert after["dispatch_owner"] is None
        assert after["error"] is None
    else:
        assert after["dispatch_state"] == before["dispatch_state"]
        assert after["dispatch_owner"] == before["dispatch_owner"]
        assert after["error"] == before["error"]
    first_advisories = [
        event for event in ProofLedger(reopened).read_events(plan_id)
        if event.stream == "advisory"
    ]
    reopened.close()
    monkeypatch.setattr(
        bestplan_proof.time,
        "time_ns",
        lambda: (_ for _ in ()).throw(
            AssertionError("an exact advisory retry must reuse its stored timestamp")
        ),
    )

    retried = BestplanStore(db_path=tmp_path / "state.db")
    retried_after = retried.get_plan(plan_id)
    assert [
        event for event in ProofLedger(retried).read_events(plan_id)
        if event.stream == "advisory"
    ] == first_advisories
    assert tuple(
        retried_after[name] for name in _TASK5_AUTHORITY_PROJECTION_FIELDS
    ) == tuple(before[name] for name in _TASK5_AUTHORITY_PROJECTION_FIELDS)
    assert retried_after["error"] == after["error"]
    assert retried_after["dispatch_state"] == after["dispatch_state"]
    assert retried_after["dispatch_owner"] == after["dispatch_owner"]


@pytest.mark.parametrize("authority_phase", ("candidate_ready", "queued"))
@pytest.mark.parametrize("tracker_status", ("scheduled", "running"))
def test_task5_reconcile_stale_active_tracker_cannot_regress_advanced_phase(
    tmp_path, monkeypatch, authority_phase, tracker_status,
):
    context = _task5_run_v2_batch(
        tmp_path, monkeypatch, promotion_mode="candidate_only",
    )
    store = context["store"]
    plan_id = context["capture"].plan_id
    assert store.mark_completed_unverified(
        plan_id,
        {"status": "completed", "results": context["combined"]["results"]},
    )
    if authority_phase == "queued":
        _task5_advance_to_queued(store, plan_id)
    before = store.get_plan(plan_id)
    assert before["dispatch_state"] == "terminal"
    assert before["dispatch_owner"] is None
    _task5_write_async_tracker(
        tmp_path, f"bestplan-{plan_id}", tracker_status,
    )
    store.close()

    reopened = BestplanStore(db_path=tmp_path / "state.db")
    after = reopened.get_plan(plan_id)

    fields = _TASK5_AUTHORITY_PROJECTION_FIELDS + (
        "error", "dispatch_state", "dispatch_owner",
    )
    assert tuple(after[name] for name in fields) == tuple(
        before[name] for name in fields
    )


def test_task5_late_dispatch_advisory_does_not_regress_candidate_ready_projection(
    tmp_path, monkeypatch,
):
    from agent.bestplan_proof import ProofLedger

    context = _task5_run_v2_batch(
        tmp_path,
        monkeypatch,
        promotion_mode="candidate_only",
        run_during_dispatch=True,
    )
    store = context["store"]
    plan_id = context["capture"].plan_id
    row = store.get_plan(plan_id)
    advisory = [
        event for event in ProofLedger(store).read_events(plan_id)
        if event.stream == "advisory"
    ][-1]

    assert row["current_phase"] == "candidate_ready"
    assert row["dispatch_state"] == "terminal"
    assert row["dispatch_owner"] is None
    assert advisory.kind == "dispatch_scheduled_advisory"
    assert advisory.phase == "candidate_ready"
    assert advisory.compatibility_dispatch_state is None


def test_task5_immediate_batch_failure_finalizer_cannot_be_regressed_to_scheduled(
    tmp_path, monkeypatch,
):
    from agent.bestplan_candidates import CandidateExecutionError
    from agent.bestplan_proof import ProofLedger

    store, capture, snapshot, runtime, authority = _task5_capture_v2(
        tmp_path,
        monkeypatch,
        promotion_mode="candidate_only",
        read_only=True,
    )
    finalized = []

    monkeypatch.setattr(
        "agent.bestplan_candidates.run_and_freeze_candidate",
        lambda **_kwargs: (_ for _ in ()).throw(
            CandidateExecutionError("candidate failed")
        ),
    )

    def fail_before_dispatch_returns(**kwargs):
        from tools.delegate_tool import BestplanCandidateBatchError

        with pytest.raises(BestplanCandidateBatchError):
            kwargs["runner"]()
        evidence = {"status": "error", "results": []}
        assert store.mark_completed_unverified(capture.plan_id, evidence)
        finalized.append(store.get_plan(capture.plan_id))
        return {
            "status": "dispatched",
            "delegation_id": kwargs["delegation_id"],
        }

    monkeypatch.setattr(
        "tools.async_delegation.dispatch_async_delegation_batch",
        fail_before_dispatch_returns,
    )
    monkeypatch.setattr(
        "gateway.session_context.async_delivery_supported", lambda: True,
    )
    monkeypatch.setattr(
        "gateway.session_context.get_delivery_context_identity",
        lambda: {
            "capability_version": 1,
            "session_key": "task5-session",
            "session_id": "task5-session",
            "ui_session_id": "task5-session",
            "profile": "coder",
            "tracker_path": str(tmp_path / "async.json"),
        },
    )

    result = try_resolve_go(
        "go",
        session_id="task5-session",
        profile="coder",
        workspace=snapshot.repo.workspace,
        baseline_fingerprint=snapshot.fingerprint,
        parent_agent=SimpleNamespace(),
        config=_config(),
        store=store,
        runtime_resolver=lambda tasks, _parent: [
            {"route": task["route"], "provider": "test", "model": "model"}
            for task in tasks
        ],
        candidate_host_runtime=runtime,
        authority_client=authority,
    )

    assert result.status == "waiting"
    assert finalized[0]["current_phase"] == "captured"
    assert finalized[0]["dispatch_state"] == "terminal"
    row = store.get_plan(capture.plan_id)
    assert row["current_phase"] == "captured"
    assert row["dispatch_state"] == "terminal"
    assert row["dispatch_owner"] is None
    assert row["error"] == "recapture_required"
    scheduling = [
        event for event in ProofLedger(store).read_events(capture.plan_id)
        if event.kind == "dispatch_scheduled_advisory"
    ][-1]
    assert scheduling.compatibility_dispatch_state is None


def test_task5_scheduled_interrupt_finalizer_cannot_be_regressed_to_deferred(
    tmp_path, monkeypatch,
):
    from agent.bestplan_proof import ProofLedger

    store, capture, snapshot, runtime, authority = _task5_capture_v2(
        tmp_path,
        monkeypatch,
        promotion_mode="candidate_only",
        read_only=True,
    )
    finalized = []

    def interrupt_before_dispatch_returns(**_kwargs):
        evidence = {"status": "interrupted", "results": []}
        assert store.mark_completed_unverified(capture.plan_id, evidence)
        finalized.append(store.get_plan(capture.plan_id))
        return {
            "status": "rejected",
            "error": "async running checkpoint timed out before execution",
        }

    monkeypatch.setattr(
        "gateway.session_context.async_delivery_supported", lambda: True,
    )

    result = try_resolve_go(
        "go",
        session_id="task5-session",
        profile="coder",
        workspace=snapshot.repo.workspace,
        baseline_fingerprint=snapshot.fingerprint,
        parent_agent=SimpleNamespace(),
        config=_config(),
        store=store,
        runtime_resolver=lambda tasks, _parent: [
            {"route": task["route"], "provider": "test", "model": "model"}
            for task in tasks
        ],
        strict_dispatcher=interrupt_before_dispatch_returns,
        candidate_host_runtime=runtime,
        authority_client=authority,
    )

    assert result.status == "dispatch_deferred"
    assert finalized[0]["current_phase"] == "captured"
    assert finalized[0]["dispatch_state"] == "terminal"
    row = store.get_plan(capture.plan_id)
    assert row["current_phase"] == "captured"
    assert row["dispatch_state"] == "terminal"
    assert row["dispatch_owner"] is None
    assert row["error"] == "recapture_required"
    deferred = [
        event for event in ProofLedger(store).read_events(capture.plan_id)
        if event.kind == "dispatch_deferred_advisory"
    ][-1]
    assert deferred.compatibility_error is None
    assert deferred.compatibility_dispatch_state is None


def test_task5_bare_go_never_redispatches_an_advanced_v2_plan(
    tmp_path, monkeypatch,
):
    from agent.bestplan_proof import ProofLedger

    context = _task5_run_v2_batch(
        tmp_path, monkeypatch, promotion_mode="candidate_only",
    )
    store = context["store"]
    capture = context["capture"]
    snapshot = context["snapshot"]
    runtime = context["runtime"]
    authority = context["authority"]
    ProofLedger(store).append_advisory(
        plan_id=capture.plan_id,
        operation_id="00000000-0000-0000-0000-000000000099",
        kind="dispatch_owner_unknown_advisory",
        raw_output={"status": "unknown"},
        output_source="host",
        compatibility_dispatch_state="unknown",
        compatibility_clear_dispatch_owner=True,
    )
    dispatched = []

    result = try_resolve_go(
        "go",
        session_id="task5-session",
        profile="coder",
        workspace=snapshot.repo.workspace,
        baseline_fingerprint=snapshot.fingerprint,
        parent_agent=SimpleNamespace(),
        config=_config(),
        store=store,
        runtime_resolver=lambda tasks, _parent: [
            {"route": task["route"], "provider": "test", "model": "model"}
            for task in tasks
        ],
        strict_dispatcher=lambda **kwargs: dispatched.append(kwargs),
        candidate_host_runtime=runtime,
        authority_client=authority,
    )

    assert result.status == "already_advanced"
    assert result.plan_id == capture.plan_id
    assert dispatched == []
    assert authority.registered == []
    assert not runtime.attempts_root.exists()


@pytest.mark.parametrize("promotion_mode", ("candidate_only", "auto_live"))
def test_task5_v2_batch_records_all_receipts_then_one_candidate_ready_and_stops(
    tmp_path, monkeypatch, promotion_mode,
):
    from agent.bestplan_proof import ProofLedger
    from agent.bestplan_redaction import redact_output

    context = _task5_run_v2_batch(
        tmp_path, monkeypatch, promotion_mode=promotion_mode,
    )
    store = context["store"]
    plan_id = context["capture"].plan_id
    row = store.get_plan(plan_id)
    candidate_rows = store._connection().execute(
        "SELECT * FROM bestplan_candidates WHERE plan_id=? ORDER BY candidate_id",
        (plan_id,),
    ).fetchall()
    events = ProofLedger(store).read_events(plan_id)
    authority_events = [event for event in events if event.stream == "authority"]
    advisory_events = [event for event in events if event.stream == "advisory"]

    assert len(candidate_rows) == 2
    assert context["persistence_order"][-1] == ("event", "candidate_ready")
    assert [kind for kind, _value in context["persistence_order"]] == [
        "candidate", "candidate", "event",
    ]
    assert context["persistence_stores"]
    assert all(item is not store for item in context["persistence_stores"])
    assert {
        item.state_db_path for item in context["persistence_stores"]
    } == {store.state_db_path}
    assert len(authority_events) == 1
    assert authority_events[0].kind == authority_events[0].phase == "candidate_ready"
    assert authority_events[0].origin == "promoter"
    assert [event.kind for event in advisory_events] == [
        "dispatch_scheduled_advisory"
    ]
    assert all(
        item["toolsets"] == item["bestplan_toolsets"] == ["file"]
        for item in context["admitted"]["resolved_runtimes"]
    )
    assert "terminal" not in json.dumps(
        _task5_thaw(context["admitted"]["resolved_runtimes"]),
        sort_keys=True,
    )
    contract = json.loads(row["promotion_contract_json"])
    assert authority_events[0].authority_epoch == contract["enrollment"]["epoch"]
    assert authority_events[0].event_seq == 1
    assert authority_events[0].previous_hash is None
    assert authority_events[0].event_hash == row["proof_event_hash"]
    assert authority_events[0].integration_oid is None
    assert authority_events[0].artifact_digest is None
    assert row["state"] == PlanState.RUNNING
    assert row["current_phase"] == "candidate_ready"
    assert row["candidate_set_digest"]
    assert row["integration_oid"] is None
    assert row["verified_at"] is None
    assert row["completed_at"] is None
    assert context["combined"]["status"] == "candidate_ready"
    assert [item["manifest_slice_id"] for item in context["combined"]["results"]] == [
        "slice first/ß",
        "slice second/ß",
    ]
    raw_by_candidate = {
        item["candidate_id"]: item["raw_receipt"]
        for item in context["raw_candidate_receipts"]
    }
    frozen_by_candidate = {
        item.candidate_id: item for item in context["frozen_candidates"]
    }
    spec_by_candidate = {
        item["spec"].candidate_id: item["spec"] for item in context["started"]
    }
    manifest_slice_ids = {
        item["candidate_id"]: item["manifest_slice_id"]
        for item in context["combined"]["results"]
    }
    assert set(raw_by_candidate) == set(frozen_by_candidate) == {
        item["candidate_id"] for item in context["combined"]["results"]
    }
    assert len({
        raw["policy_digest"] for raw in raw_by_candidate.values()
    }) == len(raw_by_candidate)
    for candidate_row in candidate_rows:
        candidate_id = candidate_row["candidate_id"]
        raw = raw_by_candidate[candidate_id]
        frozen = frozen_by_candidate[candidate_id]
        spec = spec_by_candidate[candidate_id]
        assert raw["schema"] == "hermes.bestplan.host-candidate-receipt.v1"
        assert raw["manifest_slice_id"] == manifest_slice_ids[candidate_id]
        assert raw["candidate_id"] == candidate_id == frozen.candidate_id
        assert raw["slice_id"] == frozen.slice_id
        assert raw["attempt_id"] == frozen.attempt_id
        assert raw["candidate_ref"] == frozen.ref_name
        assert raw["commit_oid"] == frozen.commit_oid
        assert raw["tree_oid"] == frozen.tree_oid
        assert raw["policy_digest"] == frozen.policy_digest
        assert raw["candidate_expires_at"] == spec.expires_at
        assert raw["promotion_contract_digest"] == row[
            "promotion_contract_digest"
        ]
        assert raw["changed_paths"] == {
            "count": len(frozen.changed_paths),
            "sha256": _task5_changed_paths_digest(frozen.changed_paths),
        }
        assert raw["controller"] == {
            "id": frozen.controller_id,
            "repository_id": frozen.controller_repository_id,
            "release_oid": frozen.controller_release_oid,
            "artifact_sha256": frozen.controller_artifact_sha256,
        }
        assert raw["admitted"] == {
            "requests": frozen.admitted_requests,
            "input_tokens": frozen.admitted_input_tokens,
            "output_tokens": frozen.admitted_output_tokens,
        }
        assert raw["worker_receipt"] == _task5_thaw(frozen.raw_receipt)
        assert raw["worker_receipt_sha256"] == frozen.raw_receipt_sha256

        projected = redact_output(raw, source="candidate")
        receipt_body = json.loads(candidate_row["receipt_json"])
        assert receipt_body["output"] == json.loads(projected.canonical_json)
        assert candidate_row["raw_output_sha256"] == projected.raw_sha256
        assert candidate_row["raw_output_framed_sha256"] == (
            projected.raw_framed_sha256
        )
        persisted = json.dumps(receipt_body["output"], sort_keys=True)
        assert frozen.ref_name not in persisted
        assert frozen.controller_id not in persisted
        assert "untrusted provider prose" not in persisted
        assert "untrusted nested prose" not in persisted
    serialized = json.dumps(context["combined"], sort_keys=True)
    assert "untrusted provider prose" not in serialized


def test_task5_v2_mixed_batch_failure_persists_no_partial_receipt_or_candidate_ready(
    tmp_path, monkeypatch,
):
    from agent.bestplan_candidates import CandidateExecutionError
    from agent.bestplan_proof import ProofLedger
    from tools import delegate_tool

    store, capture, snapshot, runtime, authority = _task5_capture_v2(
        tmp_path, monkeypatch, promotion_mode="candidate_only",
    )
    admitted = {}
    barrier = threading.Barrier(2)

    def candidate_runner(**kwargs):
        spec = kwargs["spec"]
        barrier.wait(timeout=5)
        if spec.goal.endswith("second"):
            raise CandidateExecutionError("second candidate failed")
        return _task5_frozen_candidate(
            spec, kwargs["attempt_id"], 0, runtime.controller,
        )

    monkeypatch.setattr(
        "agent.bestplan_candidates.run_and_freeze_candidate", candidate_runner,
    )
    monkeypatch.setattr(
        "tools.delegate_tool._get_max_concurrent_children", lambda: 2,
    )
    monkeypatch.setattr(
        "tools.async_delegation.dispatch_async_delegation_batch",
        lambda **kwargs: admitted.update(kwargs) or {
            "status": "dispatched",
            "delegation_id": kwargs["delegation_id"],
        },
    )
    monkeypatch.setattr(
        "gateway.session_context.async_delivery_supported", lambda: True,
    )
    monkeypatch.setattr(
        "gateway.session_context.get_delivery_context_identity",
        lambda: {
            "capability_version": 1,
            "session_key": "task5-session",
            "session_id": "task5-session",
            "ui_session_id": "task5-session",
            "profile": "coder",
            "tracker_path": str(tmp_path / "async.json"),
        },
    )

    result = try_resolve_go(
        "go",
        session_id="task5-session",
        profile="coder",
        workspace=snapshot.repo.workspace,
        baseline_fingerprint=snapshot.fingerprint,
        parent_agent=SimpleNamespace(),
        config=_config(),
        store=store,
        runtime_resolver=lambda tasks, _parent: [
            {
                "route": task["route"],
                "provider": "authority-broker",
                "model": f"model-{index}",
                "runtime_fingerprint": hashlib.sha256(
                    f"runtime-{index}".encode("ascii")
                ).hexdigest(),
            }
            for index, task in enumerate(tasks)
        ],
        candidate_host_runtime=runtime,
        authority_client=authority,
    )
    assert result.status == "waiting"

    with pytest.raises(
        delegate_tool.BestplanCandidateBatchError,
        match="candidate batch failed",
    ):
        admitted["runner"]()

    row = store.get_plan(capture.plan_id)
    assert row["current_phase"] == "captured"
    assert row["candidate_set_digest"] is None
    assert store._connection().execute(
        "SELECT COUNT(*) FROM bestplan_candidates WHERE plan_id=?",
        (capture.plan_id,),
    ).fetchone()[0] == 0
    assert [
        event for event in ProofLedger(store).read_events(capture.plan_id)
        if event.stream == "authority"
    ] == []


def test_task5_v2_late_async_advisory_changes_no_authority_or_compatibility_projection(
    tmp_path, monkeypatch,
):
    from agent.bestplan_proof import ProofLedger

    context = _task5_run_v2_batch(
        tmp_path, monkeypatch, promotion_mode="candidate_only",
    )
    store = context["store"]
    plan_id = context["capture"].plan_id
    ledger = ProofLedger(store)
    candidate_ready = [
        event for event in ledger.read_events(plan_id)
        if event.stream == "authority"
    ][-1]
    queued = ledger.append_event(
        plan_id=plan_id,
        authority_epoch=candidate_ready.authority_epoch,
        operation_id="00000000-0000-0000-0000-000000000002",
        expected_epoch=candidate_ready.authority_epoch,
        expected_seq=candidate_ready.event_seq,
        expected_hash=candidate_ready.event_hash,
        kind="queued",
        phase="queued",
        projected_state=PlanState.RUNNING,
        integration_oid=None,
        artifact_digest=None,
        origin="promoter",
        raw_output={"status": "queued"},
        output_source="promoter",
        contract_digest=store.get_plan(plan_id)["promotion_contract_digest"],
    )
    before = store.get_plan(plan_id)
    fields = (
        "state",
        "current_phase",
        "candidate_set_digest",
        "error",
        "dispatch_state",
        "integration_oid",
        "artifact_digest",
        "proof_authority_epoch",
        "proof_event_seq",
        "proof_event_hash",
        "verification_receipt_json",
        "verification_receipt_digest",
        "tests_verified_at",
        "review_verified_at",
        "remote_verified_at",
        "live_verified_at",
        "verified_at",
        "completed_at",
    )

    assert store.mark_completed_unverified(
        plan_id,
        {"status": "completed", "results": context["combined"]["results"]},
    )

    after = store.get_plan(plan_id)
    assert tuple(after[name] for name in fields) == tuple(
        before[name] for name in fields
    )
    events = ProofLedger(store).read_events(plan_id)
    authority_events = [event for event in events if event.stream == "authority"]
    advisory_events = [event for event in events if event.stream == "advisory"]
    assert authority_events[-1] == queued
    assert advisory_events[-1].phase == "queued"
    assert advisory_events[-1].kind == "async_terminal_advisory"


def test_task5_read_only_expected_artifact_is_not_reinterpreted_as_model_prose(
    tmp_path, monkeypatch,
):
    context = _task5_run_v2_batch(
        tmp_path,
        monkeypatch,
        promotion_mode="auto_live",
        read_only=True,
    )

    assert len(context["started"]) == 1
    spec = context["started"][0]["spec"]
    assert spec.read_only is True
    assert spec.allowed_paths == ()
    assert spec.toolsets == ("read_only_files",)
    assert "terminal" not in spec.toolsets
    assert spec.expected_artifacts == ("docs/existing-review.md",)
    assert context["admitted"]["resolved_runtimes"][0]["toolsets"] == [
        "read_only_files"
    ]
    assert context["admitted"]["resolved_runtimes"][0][
        "bestplan_toolsets"
    ] == ["read_only_files"]
    assert context["frozen_candidates"][0].changed_paths == ()
    assert context["raw_candidate_receipts"][0]["raw_receipt"][
        "changed_paths"
    ] == {
        "count": 0,
        "sha256": _task5_changed_paths_digest(()),
    }
    assert context["store"].get_plan(context["capture"].plan_id)[
        "current_phase"
    ] == "candidate_ready"
    assert "untrusted provider prose" not in json.dumps(
        context["combined"], sort_keys=True,
    )


def test_task5_protocol1_candidate_evidence_remains_nonterminal(tmp_path, monkeypatch):
    snapshot = _task5_snapshot(tmp_path)
    runtime, _controller = _task5_host_runtime(tmp_path, snapshot)
    store = BestplanStore(db_path=tmp_path / "state.db")

    class UnmatchedAuthority:
        @staticmethod
        def lookup_enrollment(_repo):
            return None

    authority = UnmatchedAuthority()
    monkeypatch.setattr(
        "agent.bestplan_state.strong_source_capture_supported", lambda: True,
    )
    monkeypatch.setattr(
        "agent.bestplan_state.resolve_repo_identity", lambda _workspace: snapshot.repo,
    )
    monkeypatch.setattr(
        "agent.bestplan_state.capture_source_snapshot",
        lambda _repo, _deadline: snapshot,
    )
    capture = capture_bestplan_response(
        "Protocol 1 plan\n\n" + _envelope(_task5_manifest(snapshot.repo.workspace)),
        session_id="task5-v1",
        profile="coder",
        workspace=snapshot.repo.workspace,
        store=store,
        config={
            "bestplan_promotion": {
                "authority_endpoint": "unix:///task5-authority",
                "enrollment_ref": "task5-enrollment",
            }
        },
        authority_client=authority,
    )
    assert store.get_plan(capture.plan_id)["execution_protocol"] == 1
    admitted = {}
    started = []

    def candidate_runner(**kwargs):
        started.append(kwargs)
        return _task5_frozen_candidate(
            kwargs["spec"],
            kwargs["attempt_id"],
            len(started) - 1,
            runtime.controller,
        )

    monkeypatch.setattr(
        "agent.bestplan_candidates.run_and_freeze_candidate", candidate_runner,
    )
    monkeypatch.setattr(
        "tools.async_delegation.dispatch_async_delegation_batch",
        lambda **kwargs: admitted.update(kwargs) or {
            "status": "dispatched",
            "delegation_id": kwargs["delegation_id"],
        },
    )
    monkeypatch.setattr(
        "gateway.session_context.async_delivery_supported", lambda: True,
    )
    monkeypatch.setattr(
        "gateway.session_context.get_delivery_context_identity",
        lambda: {
            "capability_version": 1,
            "session_key": "task5-v1",
            "session_id": "task5-v1",
            "ui_session_id": "task5-v1",
            "profile": "coder",
            "tracker_path": str(tmp_path / "async.json"),
        },
    )
    result = try_resolve_go(
        "go",
        session_id="task5-v1",
        profile="coder",
        workspace=snapshot.repo.workspace,
        baseline_fingerprint=snapshot.fingerprint,
        parent_agent=SimpleNamespace(),
        config=_config(),
        store=store,
        runtime_resolver=lambda tasks, _parent: [
            {"route": task["route"], "provider": "test", "model": "model"}
            for task in tasks
        ],
        candidate_host_runtime=runtime,
        authority_client=authority,
    )
    assert result.status == "waiting"
    combined = admitted["runner"]()
    assert len(combined["results"]) == 2
    assert store.mark_completed_unverified(capture.plan_id, combined)

    row = store.get_plan(capture.plan_id)
    assert row["state"] == PlanState.RUNNING
    assert row["dispatch_state"] == "terminal"
    assert row["completed_at"] is None
    assert row["verified_at"] is None
    assert store._connection().execute(
        "SELECT COUNT(*) FROM bestplan_candidates WHERE plan_id=?",
        (capture.plan_id,),
    ).fetchone()[0] == 0


def test_task5_async_candidate_batch_publishes_candidate_ready_after_execute_gate(
    monkeypatch,
):
    from tools import async_delegation

    durable_running = threading.Event()
    runner_started = threading.Event()
    release_freeze = threading.Event()
    terminal = []
    delegation_id = "task5-durable-execute-gate"

    def persist(record, **_kwargs):
        if record.get("status") == "running":
            durable_running.set()
        return True

    def runner():
        assert durable_running.is_set()
        runner_started.set()
        assert release_freeze.wait(timeout=5)
        return {
            "results": [{"status": "frozen", "summary": "host frozen"}]
        }

    monkeypatch.setattr(async_delegation, "_persist_record", persist)
    monkeypatch.setattr(
        async_delegation,
        "_persist_and_queue_terminal",
        lambda record, result, event: terminal.append((record, result, event)) or True,
    )
    result = async_delegation.dispatch_async_delegation_batch(
        goals=["freeze candidate"],
        context="Task 5 gate",
        toolsets=None,
        role="leaf",
        model="model",
        session_key="task5",
        runner=runner,
        max_async_children=8,
        delegation_id=delegation_id,
        bestplan_plan_id="bp-task5-durable-execute-gate",
    )
    try:
        assert result["status"] == "dispatched"
        assert runner_started.wait(timeout=5)
        assert durable_running.is_set()
        assert terminal == []
        release_freeze.set()
        deadline = time.monotonic() + 5
        while not terminal and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(terminal) == 1
        assert terminal[0][2]["status"] == "candidate_ready"
        from tools.process_registry import _format_async_delegation

        rendered = _format_async_delegation(terminal[0][2])
        assert "[BESTPLAN CANDIDATES READY" in rendered
        assert "BATCH COMPLETE" not in rendered
    finally:
        release_freeze.set()
        with async_delegation._records_lock:
            async_delegation._records.pop(delegation_id, None)


def test_task5_async_coordinator_exception_is_one_terminal_batch_error(monkeypatch):
    from tools import async_delegation, delegate_tool

    terminal = []
    delegation_id = "task5-coordinator-error"

    monkeypatch.setattr(async_delegation, "_persist_record", lambda *_a, **_k: True)
    monkeypatch.setattr(
        async_delegation,
        "_persist_and_queue_terminal",
        lambda record, result, event: terminal.append((record, result, event)) or True,
    )

    def runner():
        raise delegate_tool.BestplanCandidateBatchError("candidate batch failed")

    result = async_delegation.dispatch_async_delegation_batch(
        goals=["freeze candidate"],
        context="Task 5 coordinator error",
        toolsets=None,
        role="leaf",
        model="model",
        session_key="task5",
        runner=runner,
        max_async_children=8,
        delegation_id=delegation_id,
        bestplan_plan_id="bp-task5-coordinator-error",
    )
    try:
        assert result["status"] == "dispatched"
        deadline = time.monotonic() + 5
        while not terminal and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(terminal) == 1
        assert terminal[0][2]["status"] == "error"
        assert terminal[0][2]["results"] == []
    finally:
        with async_delegation._records_lock:
            async_delegation._records.pop(delegation_id, None)
