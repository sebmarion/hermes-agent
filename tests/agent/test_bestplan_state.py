"""Deterministic tests for /bestplan persisted state and go resolver."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.bestplan_state import (
    BestplanError,
    BestplanStore,
    ResolvedGo,
    _is_go_trigger,
    _plan_to_delegate_tasks,
    _v1_plan_constraints,
    compute_baseline_fingerprint,
    try_resolve_go,
)
from agent.execution_plan import ExecutionPlan, ExecutionSlice, compile_execution_plan


def _slice(slice_id: str, **overrides):
    value = {
        "id": slice_id,
        "kind": "implement",
        "goal": f"Complete {slice_id}",
        "depends_on": [],
        "capability": "fast_fallback",
        "workspace": "/repo",
        "allowed_paths": [f"src/{slice_id}.py"],
        "read_only": False,
        "expected_artifacts": [f"src/{slice_id}.py"],
        "acceptance": [f"pytest -q tests/test_{slice_id}.py"],
    }
    value.update(overrides)
    return value


def _plan(*slices, **overrides):
    value = {
        "version": 1,
        "mode": "delegate",
        "risk": "low",
        "slices": list(slices),
        "merge_policy": "Integrate dependency waves after verification.",
        "stop_condition": "All acceptance checks pass.",
        "escalation_predicates": ["verification_failed_after_local_repair"],
    }
    value.update(overrides)
    return value


def _review_slice(slice_id: str):
    return _slice(
        slice_id,
        kind="review",
        capability="frontier_review",
        read_only=True,
        allowed_paths=[],
    )


def _sota_plan(*slices, **overrides):
    return _plan(
        *slices,
        mode="sota",
        risk="high",
        escalation_predicates=["security_sensitive_request"],
        **overrides,
    )


def _enabled_config():
    return {
        "autonomy": {"go_enabled": True},
        "delegation": {
            "lanes": {
                "code_worker": {"provider": "test", "model": "coder"},
                "smart_reviewer": {"provider": "test", "model": "reviewer"},
            }
        },
    }


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / ".hermes" / "state.db")
    return BestplanStore(session_db=db)


def test_manifest_digest_is_stable_and_binds_approval(store):
    raw = compile_execution_plan(_plan(_slice("a")))
    plan_id = store.create_plan("do x", raw, session_id="s1", workspace="/tmp/ws")
    assert store.approve_plan(plan_id, approver="test")
    row = store.get_plan(plan_id)
    manifest = json.loads(row["validated_manifest_json"])
    from agent.bestplan_state import _manifest_digest

    assert row["approval_digest"] == _manifest_digest(manifest)


def test_atomic_claim_prevents_double_go(store, monkeypatch):
    raw = compile_execution_plan(_plan(_slice("a")))
    plan_id = store.create_plan("do x", raw, session_id="s1", workspace="/tmp/ws")
    store.approve_plan(plan_id)
    baseline = compute_baseline_fingerprint("/tmp/ws")

    # First claim succeeds.
    assert store.atomic_claim_approved(plan_id, baseline) is not None
    # Second claim fails (state is running).
    assert store.atomic_claim_approved(plan_id, baseline) is None


def test_go_resolver_returns_no_plan_when_none_pending(store):
    result = try_resolve_go(
        "go",
        session_id="s1",
        workspace="/tmp/ws",
        parent_agent=None,
        config=_enabled_config(),
        store=store,
    )
    assert result.resolved is False
    assert result.status == "no_plan"


def test_go_resolver_returns_ambiguous_when_multiple_approved(store):
    for i in range(2):
        raw = compile_execution_plan(_plan(_slice(f"a{i}")))
        plan_id = store.create_plan(f"do {i}", raw, session_id="s1", workspace="/tmp/ws")
        store.approve_plan(plan_id)
    result = try_resolve_go(
        "go",
        session_id="s1",
        workspace="/tmp/ws",
        parent_agent=None,
        config=_enabled_config(),
        store=store,
    )
    assert result.resolved is True
    assert result.status == "ambiguous"


def test_go_resolver_rejects_stale_baseline(store, monkeypatch):
    raw = compile_execution_plan(_plan(_slice("a")))
    plan_id = store.create_plan("do x", raw, session_id="s1", workspace="/tmp/ws")
    store.approve_plan(plan_id)
    # Mutate workspace path so baseline fingerprint differs.
    result = try_resolve_go(
        "go",
        session_id="s1",
        workspace="/tmp/other",
        parent_agent=None,
        config=_enabled_config(),
        store=store,
    )
    assert result.resolved is True
    assert result.status == "context_mismatch"


def test_go_resolver_dispatches_and_persists_delegation_id(store, monkeypatch):
    raw = compile_execution_plan(_sota_plan(_review_slice("b")))
    plan_id = store.create_plan("do x", raw, session_id="s1", workspace="/tmp/ws")
    store.approve_plan(plan_id)

    fake_agent = SimpleNamespace()
    fake_result = {
        "status": "dispatched",
        "delegation_id": "dlg_123",
        "count": 2,
    }
    monkeypatch.setattr(
        "tools.delegate_tool.delegate_task",
        lambda **kwargs: json.dumps(fake_result),
    )
    # Patch async_delivery_supported so the resolver believes delivery works.
    monkeypatch.setattr(
        "gateway.session_context.async_delivery_supported",
        lambda: True,
    )

    result = try_resolve_go(
        "go",
        session_id="s1",
        workspace="/tmp/ws",
        parent_agent=fake_agent,
        config=_enabled_config(),
        store=store,
    )
    assert result.resolved is True
    assert result.status == "waiting"
    assert result.plan_id == plan_id
    assert result.delegation_id == "dlg_123"
    row = store.get_plan(plan_id)
    assert row["state"] == "waiting"
    assert json.loads(row["delegation_ids_json"]) == ["dlg_123"]


def test_v1_rejects_more_than_two_slices(store):
    raw = compile_execution_plan(_plan(_slice("a"), _slice("b"), _slice("c")))
    with pytest.raises(BestplanError, match="at most 2 slices"):
        _v1_plan_constraints(raw)


def test_v1_rejects_dependencies(store):
    raw = compile_execution_plan(
        _plan(_slice("a"), _slice("b", depends_on=["a"]))
    )
    with pytest.raises(BestplanError, match="dependencies"):
        _v1_plan_constraints(raw)


def test_v1_only_supports_code_worker_and_smart_reviewer_lanes():
    valid_code = compile_execution_plan(_plan(_slice("a")))
    tasks = _plan_to_delegate_tasks(valid_code)
    assert len(tasks) == 1
    assert tasks[0]["route"] == "code_worker"

    valid_review = compile_execution_plan(_sota_plan(_review_slice("judge")))
    tasks = _plan_to_delegate_tasks(valid_review)
    assert tasks[0]["route"] == "smart_reviewer"


def test_missing_lane_rejection(store, monkeypatch):
    raw = compile_execution_plan(
        _plan(_slice("a", capability="local_execution"))
    )
    plan_id = store.create_plan("do x", raw, session_id="s1", workspace="/tmp/ws")
    store.approve_plan(plan_id)

    result = try_resolve_go(
        "go",
        session_id="s1",
        workspace="/tmp/ws",
        parent_agent=SimpleNamespace(),
        config=_enabled_config(),
        store=store,
    )
    assert result.resolved is True
    assert result.status == "invalid_plan"


def test_go_triggers_match_expected_phrases():
    assert _is_go_trigger("go")
    assert _is_go_trigger("  GO  ")
    assert not _is_go_trigger("GO!")
    assert not _is_go_trigger("execute the plan")
    assert not _is_go_trigger("run it")
    assert not _is_go_trigger("implement it")
    assert not _is_go_trigger("please proceed")


def test_dispatch_result_is_waiting_not_completed(store, monkeypatch):
    raw = compile_execution_plan(_plan(_slice("a")))
    plan_id = store.create_plan("do x", raw, session_id="s1", workspace="/tmp/ws")
    store.approve_plan(plan_id)

    monkeypatch.setattr(
        "tools.delegate_tool.delegate_task",
        lambda **kwargs: json.dumps({"status": "dispatched", "delegation_id": "d"}),
    )
    monkeypatch.setattr(
        "gateway.session_context.async_delivery_supported",
        lambda: True,
    )

    result = try_resolve_go(
        "go",
        session_id="s1",
        workspace="/tmp/ws",
        parent_agent=SimpleNamespace(),
        config=_enabled_config(),
        store=store,
    )
    assert result.status == "waiting"
    row = store.get_plan(plan_id)
    assert row["state"] == "waiting"


def test_two_independent_slices_dispatch_once(store, monkeypatch):
    raw = compile_execution_plan(_plan(_slice("a"), _slice("b")))
    plan_id = store.create_plan("do x", raw, session_id="s1", workspace="/tmp/ws")
    store.approve_plan(plan_id)

    calls = []

    def fake_delegate_task(**kwargs):
        calls.append(kwargs)
        return json.dumps({"status": "dispatched", "delegation_id": "d2"})

    monkeypatch.setattr("tools.delegate_tool.delegate_task", fake_delegate_task)
    monkeypatch.setattr(
        "gateway.session_context.async_delivery_supported",
        lambda: True,
    )

    result = try_resolve_go(
        "go",
        session_id="s1",
        workspace="/tmp/ws",
        parent_agent=SimpleNamespace(),
        config=_enabled_config(),
        store=store,
    )
    assert result.resolved is True
    assert result.delegation_id == "d2"
    assert len(calls) == 1
    assert len(calls[0]["tasks"]) == 2
    assert {t["route"] for t in calls[0]["tasks"]} == {"code_worker"}


def test_non_dispatched_delegate_result_fails_closed(store, monkeypatch):
    raw = compile_execution_plan(_plan(_slice("a")))
    plan_id = store.create_plan("do x", raw, session_id="s1", workspace="/tmp/ws")
    store.approve_plan(plan_id)

    monkeypatch.setattr(
        "tools.delegate_tool.delegate_task",
        lambda **kwargs: json.dumps({"results": [], "total_duration_seconds": 0}),
    )
    monkeypatch.setattr(
        "gateway.session_context.async_delivery_supported",
        lambda: True,
    )

    result = try_resolve_go(
        "go",
        session_id="s1",
        workspace="/tmp/ws",
        parent_agent=SimpleNamespace(),
        config=_enabled_config(),
        store=store,
    )
    assert result.resolved is True
    assert result.status == "dispatch_failed"
    row = store.get_plan(plan_id)
    assert row["state"] == "failed"
