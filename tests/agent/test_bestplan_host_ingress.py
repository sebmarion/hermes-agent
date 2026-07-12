from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from agent.bestplan_state import (
    BESTPLAN_ENVELOPE_END,
    BESTPLAN_ENVELOPE_START,
    BestplanStore,
    PlanState,
    capture_bestplan_response,
    compute_baseline_fingerprint,
    try_resolve_go,
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
