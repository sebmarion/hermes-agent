from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest

from agent.bestplan_local_git import (
    LOCAL_MAIN_REF,
    LocalMainLandingReceipt,
    LocalMainPushReceipt,
    LocalMainPushTarget,
    LocalPushEffectUnknown,
)
from agent.bestplan_contract import source_snapshot_digest, source_snapshot_json
from agent.bestplan_source import (
    capture_source_snapshot,
    resolve_repo_identity,
)
from agent.bestplan_state import BestplanStore, PlanState


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "BestPlan Tests")
    _git(repo, "config", "user.email", "tests@example.invalid")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    return repo


def _snapshot(repo: Path):
    return capture_source_snapshot(
        resolve_repo_identity(str(repo)),
        time.monotonic() + 20.0,
    )


def _store(tmp_path: Path) -> BestplanStore:
    return BestplanStore(db_path=tmp_path / "state" / "state.db")


def _add_compression_continuation(session_db, parent: str, child: str) -> None:
    """Create one real SessionDB compression edge with a live child."""

    base = time.time() - 10.0
    session_db.create_session(parent, source="cli")
    session_db.end_session(parent, "compression")
    session_db.create_session(child, source="cli", parent_session_id=parent)
    session_db.append_message(child, role="assistant", content="continued")
    session_db._conn.execute(
        "UPDATE sessions SET started_at=?, ended_at=? WHERE id=?",
        (base, base + 1.0, parent),
    )
    session_db._conn.execute(
        "UPDATE sessions SET started_at=? WHERE id=?",
        (base + 2.0, child),
    )
    session_db._conn.commit()
    assert session_db.resolve_resume_session_id(parent) == child


def _seed_local_plan(
    store: BestplanStore,
    snapshot,
    _integration_oid: str,
    *,
    plan_id: str = "bp-local",
    session_id: str = "session-1",
    profile: str = "coder",
    active_dispatch: bool = False,
) -> None:
    """Seed one fully bound local-go plan without running a model."""

    from agent.bestplan_contract import BoundCommand, ControllerIdentity
    from agent.bestplan_local import (
        build_local_go_contract,
        local_go_approval_digest,
        local_go_contract_digest,
        local_go_contract_json,
        local_go_manifest_digest,
    )
    from agent.bestplan_state import BESTPLAN_ENVELOPE_END, BESTPLAN_ENVELOPE_START
    from agent.execution_plan import compile_execution_plan

    source_json = source_snapshot_json(snapshot)
    source_digest = source_snapshot_digest(snapshot)
    plan = compile_execution_plan(
        {
            "version": 1,
            "mode": "delegate",
            "risk": "low",
            "slices": [
                {
                    "id": "implement",
                    "kind": "implement",
                    "goal": "Implement the approved change",
                    "depends_on": [],
                    "capability": "fast_fallback",
                    "workspace": snapshot.repo.workspace,
                    "allowed_paths": ["feature.py"],
                    "read_only": False,
                    "expected_artifacts": ["feature.py"],
                    "acceptance": ["pytest -q"],
                }
            ],
            "merge_policy": "Integrate after exact checks.",
            "stop_condition": "All exact checks pass.",
            "escalation_predicates": ["verification_failed_after_local_repair"],
        }
    )
    manifest = plan.to_manifest()
    controller = ControllerIdentity(
        repository_id=snapshot.repo.repository_id,
        controller_id="local-test-controller",
        release_oid=snapshot.head_oid,
        artifact_sha256="4" * 64,
    )
    command = BoundCommand(
        identifier="pytest",
        executable="/usr/bin/true",
        executable_sha256="5" * 64,
        argv=(),
        logical_cwd="integration",
        env=(),
        inputs=(),
        cache=(),
        timeout_seconds=60,
        network_allowlist=(),
    )
    contract = build_local_go_contract(
        snapshot=snapshot,
        controller=controller,
        commands=(command,),
        manifest_digest=local_go_manifest_digest(manifest),
        check_runtime_digest="6" * 64,
    )
    contract_json = local_go_contract_json(contract)
    manifest_json = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    envelope = (
        f"{BESTPLAN_ENVELOPE_START}\n"
        + json.dumps(
            {"version": 1, "manifest": manifest},
            ensure_ascii=False,
            sort_keys=True,
        )
        + f"\n{BESTPLAN_ENVELOPE_END}"
    )
    dispatch_id = f"bestplan-{plan_id}" if active_dispatch else None
    dispatch_time = time.time() if active_dispatch else None

    def insert(conn):
        conn.execute(
            """INSERT INTO bestplan_plans (
                plan_id, version, created_at, session_id, profile, workspace,
                baseline_revision, baseline_fingerprint, raw_request,
                raw_plan_json, validated_manifest_json, state,
                approval_digest, execution_protocol, promotion_contract_version,
                promotion_contract_json, promotion_contract_digest,
                promotion_mode, source_snapshot_json, source_snapshot_digest,
                current_phase, dispatch_id, dispatch_state, dispatch_owner,
                dispatch_started_at, dispatch_updated_at,
                resolved_runtime_json, delegation_ids_json
            ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, '', ?, ?, 'running',
                      ?, 2, 1, ?, ?, 'local_main', ?, ?,
                      'captured', ?, ?, ?, ?, ?, ?, ?)""",
            (
                plan_id,
                time.time(),
                session_id,
                profile,
                snapshot.repo.workspace,
                snapshot.head_oid,
                snapshot.fingerprint,
                envelope,
                manifest_json,
                local_go_approval_digest(manifest, contract),
                contract_json,
                local_go_contract_digest(contract),
                source_json,
                source_digest,
                dispatch_id,
                "dispatching" if active_dispatch else None,
                f"pid:{os.getpid()}" if active_dispatch else None,
                dispatch_time,
                dispatch_time,
                "[]" if active_dispatch else None,
                json.dumps([dispatch_id]) if active_dispatch else None,
            ),
        )

    store._execute_write(insert)


def _target(snapshot, integration_oid: str) -> LocalMainPushTarget:
    return LocalMainPushTarget(
        remote_name="sebmarion",
        remote_ref=LOCAL_MAIN_REF,
        display_url="ssh://git.example.invalid/project.git",
        remote_identity_sha256=hashlib.sha256(b"exact remote").hexdigest(),
        observed_remote_oid=snapshot.head_oid,
        integration_oid=integration_oid,
    )


def _prepare(
    store: BestplanStore,
    snapshot,
    integration_oid: str,
    *,
    expires_at: int | None = None,
):
    return store.prepare_local_push(
        "bp-local",
        session_id="session-1",
        profile="coder",
        workspace=snapshot.repo.workspace,
        expected_target_oid=snapshot.head_oid,
        integration_oid=integration_oid,
        check_set_digest="c" * 64,
        target=_target(snapshot, integration_oid),
        expires_at=expires_at or int(time.time()) + 600,
    )


def _activate(
    store: BestplanStore,
    snapshot,
    integration_oid: str,
) -> bool:
    return store.activate_local_push(
        "bp-local",
        landing_receipt=LocalMainLandingReceipt(
            target_ref=LOCAL_MAIN_REF,
            old_oid=snapshot.head_oid,
            new_oid=integration_oid,
            check_receipt_digest="c" * 64,
        ),
    )


def _finalize_prepared_and_reopen(store: BestplanStore) -> BestplanStore:
    """Simulate the async wrapper failing after the Git effect was prepared."""

    assert store.mark_completed_unverified(
        "bp-local",
        {
            "status": "error",
            "error": "candidate_batch_failed",
            "results": [],
        },
    )
    state_path = store.state_db_path
    store.close()
    return BestplanStore(db_path=state_path, reconcile_push_state=False)


def test_prepare_is_canonical_context_bound_and_durable_before_local_effect(tmp_path):
    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    integration_oid = "1" * len(snapshot.head_oid)
    store = _store(tmp_path)
    _seed_local_plan(store, snapshot, integration_oid)

    prepared = _prepare(store, snapshot, integration_oid)

    assert prepared["state"] == "prepared"
    assert _git(repo, "rev-parse", "HEAD") == snapshot.head_oid
    row = store.get_plan("bp-local")
    assert row["local_push_state"] == "prepared"
    raw = row["local_push_json"]
    decoded = json.loads(raw)
    assert raw == json.dumps(
        decoded,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert decoded["schema"] == "hermes.bestplan.local-push.v1"
    assert decoded["plan_id"] == "bp-local"
    assert decoded["session_id"] == "session-1"
    assert decoded["profile"] == "coder"
    assert decoded["workspace"] == snapshot.repo.workspace
    assert decoded["repository"]["repository_id"] == snapshot.repo.repository_id
    assert decoded["expected_target_oid"] == snapshot.head_oid
    assert decoded["integration_oid"] == integration_oid
    assert decoded["check_set_digest"] == "c" * 64
    assert decoded["remote_identity_sha256"] == _target(
        snapshot, integration_oid,
    ).remote_identity_sha256
    assert "credential" not in raw.casefold()
    assert "password" not in raw.casefold()

    state_path = store.state_db_path
    store.close()
    with sqlite3.connect(state_path) as connection:
        durable = connection.execute(
            "SELECT local_push_json, local_push_state FROM bestplan_plans "
            "WHERE plan_id='bp-local'"
        ).fetchone()
    assert durable == (raw, "prepared")


def test_activate_requires_the_exact_postflight_landing_receipt(tmp_path):
    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    integration_oid = "1" * len(snapshot.head_oid)
    store = _store(tmp_path)
    _seed_local_plan(store, snapshot, integration_oid)
    _prepare(store, snapshot, integration_oid)

    assert not store.activate_local_push(
        "bp-local",
        landing_receipt=LocalMainLandingReceipt(
            target_ref=LOCAL_MAIN_REF,
            old_oid=snapshot.head_oid,
            new_oid="2" * len(snapshot.head_oid),
            check_receipt_digest="c" * 64,
        ),
    )
    assert store.get_plan("bp-local")["local_push_state"] == "prepared"

    assert _activate(store, snapshot, integration_oid)
    row = store.get_plan("bp-local")
    assert row["local_push_state"] == "awaiting"
    assert row["state"] == PlanState.COMPLETED_LOCAL
    assert row["current_phase"] == "captured"
    assert row["integration_oid"] is None
    landed = json.loads(row["local_push_json"])
    assert landed["integration_oid"] == integration_oid
    assert landed["check_set_digest"] == "c" * 64
    assert not _activate(store, snapshot, integration_oid)


def test_async_finalizer_after_activation_preserves_awaiting_push_proof(tmp_path):
    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    integration_oid = "1" * len(snapshot.head_oid)
    store = _store(tmp_path)
    _seed_local_plan(store, snapshot, integration_oid)
    _prepare(store, snapshot, integration_oid)
    assert _activate(store, snapshot, integration_oid)
    before = store.get_plan("bp-local")

    assert store.mark_completed_unverified(
        "bp-local", {"status": "completed", "untrusted": "async wrapper"},
    )

    after = store.get_plan("bp-local")
    assert after["local_push_json"] == before["local_push_json"]
    assert after["local_push_state"] == "awaiting"
    assert after["current_phase"] == before["current_phase"]
    assert after["state"] == before["state"]
    assert after["state"] == PlanState.COMPLETED_LOCAL
    assert after["error"] == before["error"]


def test_async_finalizer_preserves_a_prepared_local_effect_for_readback(tmp_path):
    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    integration_oid = "1" * len(snapshot.head_oid)
    store = _store(tmp_path)
    _seed_local_plan(store, snapshot, integration_oid, active_dispatch=True)
    _prepare(store, snapshot, integration_oid)

    assert store.mark_completed_unverified(
        "bp-local",
        {
            "status": "error",
            "error": "candidate_batch_failed",
            "results": [],
        },
    )

    row = store.get_plan("bp-local")
    assert row["state"] == PlanState.RUNNING
    assert row["local_push_state"] == "prepared"
    assert row["dispatch_owner"] is None


def test_reopen_recovers_a_prepared_exact_landing_as_awaiting(tmp_path):
    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    integration_oid = "1" * len(snapshot.head_oid)
    store = _store(tmp_path)
    _seed_local_plan(store, snapshot, integration_oid, active_dispatch=True)
    _prepare(store, snapshot, integration_oid)
    store = _finalize_prepared_and_reopen(store)

    changed = store.reconcile_local_pushes(
        classify_local_main=lambda **_kwargs: "integration",
    )

    row = store.get_plan("bp-local")
    assert changed == 1
    assert row["state"] == PlanState.COMPLETED_LOCAL
    assert row["local_push_state"] == "awaiting"
    assert row["dispatch_state"] == "terminal"
    assert row["dispatch_owner"] is None


def test_reopen_keeps_an_unproved_prepared_effect_active(tmp_path):
    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    integration_oid = "1" * len(snapshot.head_oid)
    store = _store(tmp_path)
    _seed_local_plan(store, snapshot, integration_oid, active_dispatch=True)
    _prepare(store, snapshot, integration_oid)
    store = _finalize_prepared_and_reopen(store)

    changed = store.reconcile_local_pushes(
        classify_local_main=lambda **_kwargs: "expected",
    )

    row = store.get_plan("bp-local")
    assert changed == 0
    assert row["state"] == PlanState.RUNNING
    assert row["local_push_state"] == "prepared"
    assert row["dispatch_owner"] is None


def test_reopen_keeps_an_ambiguous_prepared_effect_active(tmp_path):
    for classification in ("other", "unavailable"):
        case_root = tmp_path / classification
        case_root.mkdir()
        repo = _repo(case_root)
        snapshot = _snapshot(repo)
        integration_oid = "1" * len(snapshot.head_oid)
        store = _store(case_root)
        _seed_local_plan(
            store, snapshot, integration_oid, active_dispatch=True,
        )
        _prepare(store, snapshot, integration_oid)
        store = _finalize_prepared_and_reopen(store)

        changed = store.reconcile_local_pushes(
            classify_local_main=(
                lambda result=classification, **_kwargs: result
            ),
        )

        row = store.get_plan("bp-local")
        assert changed == 0
        assert row["state"] == PlanState.RUNNING
        assert row["local_push_state"] == "prepared"
        assert row["dispatch_owner"] is None
        store.close()


def test_reopen_records_an_expired_exact_landing_before_expiring_prompt(tmp_path):
    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    integration_oid = "1" * len(snapshot.head_oid)
    store = _store(tmp_path)
    _seed_local_plan(store, snapshot, integration_oid, active_dispatch=True)
    _prepare(
        store,
        snapshot,
        integration_oid,
        expires_at=int(time.time()) + 1,
    )
    store = _finalize_prepared_and_reopen(store)

    changed = store.reconcile_local_pushes(
        classify_local_main=lambda **_kwargs: "integration",
        now=time.time() + 10,
    )

    row = store.get_plan("bp-local")
    assert changed >= 1
    assert row["state"] == PlanState.COMPLETED_LOCAL
    assert row["local_push_state"] == "expired"
    assert row["dispatch_state"] == "terminal"
    assert row["dispatch_owner"] is None


def test_reconciliation_does_not_misclassify_a_live_prepared_effect(tmp_path):
    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    integration_oid = "1" * len(snapshot.head_oid)
    store = _store(tmp_path)
    _seed_local_plan(store, snapshot, integration_oid)
    _prepare(store, snapshot, integration_oid)
    store._execute_write(
        lambda conn: conn.execute(
            "UPDATE bestplan_plans SET dispatch_owner=? WHERE plan_id='bp-local'",
            (f"pid:{os.getpid()}",),
        )
    )
    calls = []

    changed = store.reconcile_local_pushes(
        classify_local_main=lambda **kwargs: calls.append(kwargs) or "expected",
    )

    assert changed == 0
    assert calls == []
    assert store.get_plan("bp-local")["local_push_state"] == "prepared"


@pytest.mark.parametrize(
    ("owner_liveness", "expected_state", "expected_local_calls"),
    (
        (False, "awaiting", 1),
        (True, "prepared", 0),
        (None, "prepared", 0),
    ),
)
def test_session_store_uses_tracker_start_identity_before_prepared_readback(
    tmp_path,
    monkeypatch,
    owner_liveness,
    expected_state,
    expected_local_calls,
):
    from hermes_state import SessionDB
    from tools import async_delegation

    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    integration_oid = "1" * len(snapshot.head_oid)
    session_db = SessionDB(db_path=tmp_path / "state.db")
    store = BestplanStore(
        session_db=session_db,
        reconcile_push_state=False,
    )
    _seed_local_plan(
        store,
        snapshot,
        integration_oid,
        active_dispatch=True,
    )
    _prepare(store, snapshot, integration_oid)
    row = store.get_plan("bp-local")
    delegation_id = str(row["dispatch_id"])
    assert row["dispatch_owner"] == f"pid:{os.getpid()}"
    assert row["dispatch_state"] == "dispatching"
    (tmp_path / "async_delegations.json").write_text(
        json.dumps({
            "version": 1,
            "records": {
                delegation_id: {
                    "status": "running",
                    "record": {
                        "delegation_id": delegation_id,
                        "status": "running",
                        "owner_pid": os.getpid(),
                        "owner_started_at": 1,
                    },
                },
            },
        }),
        encoding="utf-8",
    )
    observed_records = []
    monkeypatch.setattr(
        async_delegation,
        "_owner_liveness",
        lambda record: observed_records.append(record) or owner_liveness,
    )

    reopened = BestplanStore(
        session_db=session_db,
        reconcile_push_state=False,
    )
    after_tracker = reopened.get_plan("bp-local")
    if owner_liveness is False:
        assert after_tracker["dispatch_state"] == "terminal"
        proof_events = session_db._conn.execute(
            "SELECT kind, compatibility_dispatch_state, "
            "compatibility_clear_dispatch_owner "
            "FROM bestplan_proof_events WHERE plan_id='bp-local' "
            "ORDER BY event_seq"
        ).fetchall()
        assert [tuple(event) for event in proof_events] == [
            ("async_tracker_lost_advisory", "terminal", 1),
        ]
        assert after_tracker["dispatch_owner"] is None
    else:
        assert after_tracker["dispatch_owner"] == f"pid:{os.getpid()}"
    local_calls = []
    reopened.reconcile_local_pushes(
        classify_local_main=(
            lambda **kwargs: local_calls.append(kwargs) or "integration"
        ),
    )

    after = reopened.get_plan("bp-local")
    assert len(observed_records) == 1
    assert after["local_push_state"] == expected_state
    assert len(local_calls) == expected_local_calls
    if owner_liveness is False:
        assert after["dispatch_owner"] is None
        assert after["state"] == PlanState.COMPLETED_LOCAL
    else:
        assert after["dispatch_owner"] == f"pid:{os.getpid()}"
        assert after["state"] == PlanState.RUNNING
    session_db.close()


def test_exact_no_is_single_use_and_performs_zero_git_or_network(
    tmp_path, monkeypatch,
):
    from agent.bestplan_local_push import try_resolve_local_push
    from agent import bestplan_local_git, bestplan_state

    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    integration_oid = "1" * len(snapshot.head_oid)
    store = _store(tmp_path)
    _seed_local_plan(store, snapshot, integration_oid)
    _prepare(store, snapshot, integration_oid)
    _activate(store, snapshot, integration_oid)
    state_path = store.state_db_path
    store.close()

    calls = []

    def forbidden_push(**kwargs):
        calls.append(kwargs)
        raise AssertionError("no must not touch Git or the network")

    monkeypatch.setattr(
        bestplan_local_git, "classify_local_main_for_push", forbidden_push,
    )
    monkeypatch.setattr(
        bestplan_local_git, "classify_local_push_remote", forbidden_push,
    )
    constructor_flags = []

    class NoReconcileStore(BestplanStore):
        def __init__(self, **kwargs):
            constructor_flags.append(kwargs.get("reconcile_push_state"))
            super().__init__(db_path=state_path, **kwargs)

    monkeypatch.setattr(bestplan_state, "BestplanStore", NoReconcileStore)

    result = try_resolve_local_push(
        "  NO  ",
        session_id="session-1",
        profile="coder",
        workspace=snapshot.repo.workspace,
        push_fn=forbidden_push,
    )

    assert result.resolved is True
    assert result.status == "push_declined"
    assert calls == []
    with sqlite3.connect(state_path) as connection:
        state = connection.execute(
            "SELECT local_push_state FROM bestplan_plans WHERE plan_id='bp-local'"
        ).fetchone()[0]
    assert state == "declined"
    second = try_resolve_local_push(
        "no",
        session_id="session-1",
        profile="coder",
        workspace=snapshot.repo.workspace,
        push_fn=forbidden_push,
    )
    assert second.resolved is False
    assert calls == []
    assert constructor_flags == [False, False]


def test_push_claim_is_atomic_and_effect_unknown_is_exactly_retryable(tmp_path):
    from agent.bestplan_local_push import try_resolve_local_push

    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    integration_oid = "1" * len(snapshot.head_oid)
    store = _store(tmp_path)
    _seed_local_plan(store, snapshot, integration_oid)
    _prepare(store, snapshot, integration_oid)
    _activate(store, snapshot, integration_oid)

    entered = threading.Event()
    release = threading.Event()
    calls = []

    def uncertain_push(**kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(timeout=5)
        raise LocalPushEffectUnknown("read-back unavailable")

    first_result = []
    thread = threading.Thread(
        target=lambda: first_result.append(
            try_resolve_local_push(
                "push",
                session_id="session-1",
                profile="coder",
                workspace=snapshot.repo.workspace,
                store=store,
                push_fn=uncertain_push,
            )
        ),
    )
    thread.start()
    assert entered.wait(timeout=5)
    concurrent = try_resolve_local_push(
        "push",
        session_id="session-1",
        profile="coder",
        workspace=snapshot.repo.workspace,
        store=store,
        push_fn=uncertain_push,
    )
    assert concurrent.resolved is True
    assert concurrent.status == "push_in_flight"
    assert len(calls) == 1
    release.set()
    thread.join(timeout=5)
    assert first_result[0].status == "push_effect_unknown"
    assert store.get_plan("bp-local")["local_push_state"] == "effect_unknown"

    def verified_readback(**_kwargs):
        return LocalMainPushReceipt(
            remote_name="sebmarion",
            remote_ref=LOCAL_MAIN_REF,
            integration_oid=integration_oid,
            remote_oid=integration_oid,
        )

    recovered = try_resolve_local_push(
        "push",
        session_id="session-1",
        profile="coder",
        workspace=snapshot.repo.workspace,
        store=store,
        push_fn=verified_readback,
    )
    assert recovered.status == "push_complete"
    assert store.get_plan("bp-local")["local_push_state"] == "pushed"


def test_restart_reconciles_remote_effect_before_receipt_without_second_effect(
    tmp_path,
):
    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    integration_oid = "1" * len(snapshot.head_oid)
    store = _store(tmp_path)
    _seed_local_plan(store, snapshot, integration_oid)
    _prepare(store, snapshot, integration_oid)
    _activate(store, snapshot, integration_oid)
    assert store.claim_local_push("bp-local", now=time.time()) is not None
    assert store.get_plan("bp-local")["local_push_state"] == "pushing"

    changed = store.reconcile_local_pushes(
        classify_local_main=lambda **_kwargs: "integration",
        classify_remote=lambda **_kwargs: "integration",
    )

    assert changed == 1
    assert store.get_plan("bp-local")["local_push_state"] == "pushed"


def test_expiry_context_repository_drift_and_nonbare_replies_fail_closed(tmp_path):
    from agent.bestplan_local_push import try_resolve_local_push

    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    integration_oid = "1" * len(snapshot.head_oid)
    store = _store(tmp_path)
    _seed_local_plan(store, snapshot, integration_oid)
    _prepare(store, snapshot, integration_oid, expires_at=int(time.time()) + 1)
    _activate(store, snapshot, integration_oid)

    calls = []
    for text in ("push now", "yes", "no thanks", "/push"):
        result = try_resolve_local_push(
            text,
            session_id="session-1",
            profile="coder",
            workspace=snapshot.repo.workspace,
            store=store,
            push_fn=lambda **kwargs: calls.append(kwargs),
        )
        assert result.resolved is False
    assert calls == []

    wrong_context = try_resolve_local_push(
        "push",
        session_id="session-1",
        profile="other-profile",
        workspace=snapshot.repo.workspace,
        store=store,
        push_fn=lambda **kwargs: calls.append(kwargs),
    )
    assert wrong_context.resolved is True
    assert wrong_context.status == "push_context_mismatch"
    assert calls == []

    expired = try_resolve_local_push(
        "push",
        session_id="session-1",
        profile="coder",
        workspace=snapshot.repo.workspace,
        store=store,
        push_fn=lambda **kwargs: calls.append(kwargs),
        now=time.time() + 10,
    )
    assert expired.resolved is True
    assert expired.status == "push_expired"
    assert calls == []
    assert store.get_plan("bp-local")["local_push_state"] == "expired"

    row = store.get_plan("bp-local")
    record = json.loads(row["local_push_json"])
    record["repository"]["repository_id"] = "f" * 64
    record["expires_at"] = int(time.time()) + 600
    store._execute_write(
        lambda conn: conn.execute(
            "UPDATE bestplan_plans SET local_push_state='awaiting', "
            "local_push_json=? WHERE plan_id='bp-local'",
            (
                json.dumps(
                    record,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
    )
    drifted = try_resolve_local_push(
        "push",
        session_id="session-1",
        profile="coder",
        workspace=snapshot.repo.workspace,
        store=store,
        push_fn=lambda **kwargs: calls.append(kwargs),
    )
    assert drifted.resolved is True
    assert drifted.status == "push_stale"
    assert calls == []


def test_restart_recovers_exact_awaiting_prompt_read_only_without_git(
    tmp_path, monkeypatch,
):
    from agent import bestplan_local_git, bestplan_state
    from agent.bestplan_local_push import recover_local_push_prompt

    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    integration_oid = "1" * len(snapshot.head_oid)
    store = _store(tmp_path)
    _seed_local_plan(store, snapshot, integration_oid)
    _prepare(store, snapshot, integration_oid)
    assert _activate(store, snapshot, integration_oid)
    state_path = store.state_db_path
    before = store.get_plan("bp-local")
    store.close()

    calls = []

    def forbidden_git(**kwargs):
        calls.append(kwargs)
        raise AssertionError("prompt recovery must not touch Git or the network")

    monkeypatch.setattr(
        bestplan_local_git, "classify_local_main_for_push", forbidden_git,
    )
    monkeypatch.setattr(
        bestplan_local_git, "classify_local_push_remote", forbidden_git,
    )
    constructor_flags = []

    class NoReconcileStore(BestplanStore):
        def __init__(self, **kwargs):
            constructor_flags.append(kwargs.get("reconcile_push_state"))
            super().__init__(db_path=state_path, **kwargs)

    monkeypatch.setattr(bestplan_state, "BestplanStore", NoReconcileStore)
    expected = (
        f"Local `main` is now `{integration_oid}` and approved checks "
        "passed. Push this exact commit to "
        "`ssh://git.example.invalid/project.git` `refs/heads/main`? "
        "Reply `push` or `no`."
    )
    kwargs = {
        "session_id": "session-1",
        "profile": "coder",
        "workspace": snapshot.repo.workspace,
        "now": time.time(),
    }

    assert recover_local_push_prompt(**kwargs) == expected
    assert recover_local_push_prompt(**kwargs) == expected

    assert constructor_flags == [False, False]
    assert calls == []
    with sqlite3.connect(state_path) as connection:
        after = dict(
            zip(
                before,
                connection.execute(
                    "SELECT * FROM bestplan_plans WHERE plan_id='bp-local'"
                ).fetchone(),
                strict=True,
            )
        )
    assert after == before


def test_compressed_continuation_recovers_ancestor_durable_push_prompt(
    tmp_path,
):
    from agent.bestplan_local_push import recover_local_push_prompt
    from hermes_state import SessionDB

    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    integration_oid = "1" * len(snapshot.head_oid)
    session_db = SessionDB(db_path=tmp_path / "state" / "state.db")
    store = BestplanStore(session_db=session_db)
    _seed_local_plan(store, snapshot, integration_oid, session_id="session-1")
    assert _prepare(store, snapshot, integration_oid)
    assert _activate(store, snapshot, integration_oid)
    _add_compression_continuation(session_db, "session-1", "session-2")

    expected = (
        f"Local `main` is now `{integration_oid}` and approved checks passed. "
        "Push this exact commit to `ssh://git.example.invalid/project.git` "
        "`refs/heads/main`? Reply `push` or `no`."
    )
    prompt = recover_local_push_prompt(
        session_id="session-2",
        profile="coder",
        workspace=snapshot.repo.workspace,
        store=store,
        now=time.time(),
    )
    unrelated = recover_local_push_prompt(
        session_id="unrelated-session",
        profile="coder",
        workspace=snapshot.repo.workspace,
        store=store,
        now=time.time(),
    )

    assert prompt == expected
    assert unrelated is None
    assert store.get_plan("bp-local")["session_id"] == "session-1"
    session_db.close()


@pytest.mark.parametrize(
    ("reply", "expected_status", "expected_state", "expected_push_calls"),
    (
        ("no", "push_declined", "declined", 0),
        ("push", "push_complete", "pushed", 1),
    ),
)
def test_compressed_continuation_consumes_ancestor_push_decision_once(
    tmp_path,
    reply,
    expected_status,
    expected_state,
    expected_push_calls,
):
    from agent.bestplan_local_push import try_resolve_local_push
    from hermes_state import SessionDB

    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    integration_oid = "1" * len(snapshot.head_oid)
    session_db = SessionDB(db_path=tmp_path / "state" / "state.db")
    store = BestplanStore(session_db=session_db)
    _seed_local_plan(store, snapshot, integration_oid, session_id="session-1")
    assert _prepare(store, snapshot, integration_oid)
    assert _activate(store, snapshot, integration_oid)
    _add_compression_continuation(session_db, "session-1", "session-2")
    push_calls = []

    def push(**kwargs):
        push_calls.append(kwargs)
        return LocalMainPushReceipt(
            remote_name="sebmarion",
            remote_ref=LOCAL_MAIN_REF,
            integration_oid=integration_oid,
            remote_oid=integration_oid,
        )

    unrelated = try_resolve_local_push(
        reply,
        session_id="unrelated-session",
        profile="coder",
        workspace=snapshot.repo.workspace,
        store=store,
        push_fn=push,
        now=time.time(),
    )
    result = try_resolve_local_push(
        reply,
        session_id="session-2",
        profile="coder",
        workspace=snapshot.repo.workspace,
        store=store,
        push_fn=push,
        now=time.time(),
    )

    assert unrelated.resolved is False
    assert unrelated.status == "no_push_prompt"
    assert result.resolved is True
    assert result.status == expected_status
    assert result.plan_id == "bp-local"
    assert len(push_calls) == expected_push_calls
    row = store.get_plan("bp-local")
    assert row["local_push_state"] == expected_state
    assert row["session_id"] == "session-1"
    session_db.close()


def test_restart_recovers_prepared_exact_landing_with_bounded_local_readback(
    tmp_path, monkeypatch,
):
    from agent import bestplan_local_git
    from agent.bestplan_local_push import recover_local_push_prompt

    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    integration_oid = "1" * len(snapshot.head_oid)
    store = _store(tmp_path)
    _seed_local_plan(
        store,
        snapshot,
        integration_oid,
        active_dispatch=True,
    )
    _prepare(store, snapshot, integration_oid)
    store = _finalize_prepared_and_reopen(store)
    before = store.get_plan("bp-local")
    assert before["state"] == PlanState.RUNNING
    assert before["local_push_state"] == "prepared"
    assert before["dispatch_owner"] is None

    remote_calls = []

    def forbidden_remote(**kwargs):
        remote_calls.append(kwargs)
        raise AssertionError("startup prompt recovery must stay local")

    monkeypatch.setattr(
        bestplan_local_git,
        "classify_local_push_remote",
        forbidden_remote,
    )
    monkeypatch.setattr(bestplan_local_git, "_read_remote_oid", forbidden_remote)
    local_calls = []
    caller_deadline = time.monotonic() + 1.0

    def classify_local(**kwargs):
        local_calls.append(kwargs)
        assert time.monotonic() < kwargs["deadline"] <= caller_deadline
        return "integration"

    prompt = recover_local_push_prompt(
        session_id="session-1",
        profile="coder",
        workspace=snapshot.repo.workspace,
        store=store,
        now=time.time(),
        deadline=caller_deadline,
        classify_local_main=classify_local,
    )

    assert prompt == (
        f"Local `main` is now `{integration_oid}` and approved checks passed. "
        "Push this exact commit to `ssh://git.example.invalid/project.git` "
        "`refs/heads/main`? Reply `push` or `no`."
    )
    assert len(local_calls) == 1
    assert local_calls[0]["expected_target_oid"] == snapshot.head_oid
    assert local_calls[0]["integration_oid"] == integration_oid
    assert remote_calls == []
    after = store.get_plan("bp-local")
    assert after["state"] == PlanState.COMPLETED_LOCAL
    assert after["local_push_state"] == "awaiting"
    assert after["dispatch_state"] == "terminal"
    assert after["dispatch_owner"] is None


def test_restart_records_expired_prepared_landing_before_expiring_prompt(
    tmp_path, monkeypatch,
):
    from agent import bestplan_local_git
    from agent.bestplan_local_push import recover_local_push_prompt

    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    integration_oid = "1" * len(snapshot.head_oid)
    store = _store(tmp_path)
    _seed_local_plan(
        store,
        snapshot,
        integration_oid,
        active_dispatch=True,
    )
    _prepare(
        store,
        snapshot,
        integration_oid,
        expires_at=int(time.time()) + 1,
    )
    store = _finalize_prepared_and_reopen(store)
    before = store.get_plan("bp-local")
    assert before["state"] == PlanState.RUNNING
    assert before["local_push_state"] == "prepared"
    assert before["dispatch_owner"] is None

    remote_calls = []

    def forbidden_remote(**kwargs):
        remote_calls.append(kwargs)
        raise AssertionError("expired startup recovery must stay local")

    monkeypatch.setattr(
        bestplan_local_git,
        "classify_local_push_remote",
        forbidden_remote,
    )
    monkeypatch.setattr(bestplan_local_git, "_read_remote_oid", forbidden_remote)
    transitions = []
    real_transition = store._set_local_push_state

    def observed_transition(plan_id, **kwargs):
        changed = real_transition(plan_id, **kwargs)
        row = store.get_plan(plan_id)
        transitions.append((
            kwargs["expected_state"],
            kwargs["new_state"],
            changed,
            row["state"],
            row["local_push_state"],
        ))
        return changed

    monkeypatch.setattr(store, "_set_local_push_state", observed_transition)
    local_calls = []
    caller_deadline = time.monotonic() + 1.0

    prompt = recover_local_push_prompt(
        session_id="session-1",
        profile="coder",
        workspace=snapshot.repo.workspace,
        store=store,
        now=time.time() + 10.0,
        deadline=caller_deadline,
        classify_local_main=(
            lambda **kwargs: local_calls.append(kwargs) or "integration"
        ),
    )

    assert prompt is None
    assert len(local_calls) == 1
    assert time.monotonic() < local_calls[0]["deadline"] <= caller_deadline
    assert remote_calls == []
    assert transitions == [
        (
            "prepared",
            "awaiting",
            True,
            PlanState.COMPLETED_LOCAL,
            "awaiting",
        ),
        (
            "awaiting",
            "expired",
            True,
            PlanState.COMPLETED_LOCAL,
            "expired",
        ),
    ]
    after = store.get_plan("bp-local")
    assert after["state"] == PlanState.COMPLETED_LOCAL
    assert after["local_push_state"] == "expired"
    assert after["dispatch_state"] == "terminal"
    assert after["dispatch_owner"] is None


def test_restart_does_not_reconcile_a_live_prepared_effect(tmp_path):
    from agent.bestplan_local_push import recover_local_push_prompt

    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    integration_oid = "1" * len(snapshot.head_oid)
    store = _store(tmp_path)
    _seed_local_plan(
        store,
        snapshot,
        integration_oid,
        active_dispatch=True,
    )
    _prepare(store, snapshot, integration_oid)
    before = store.get_plan("bp-local")
    assert before["dispatch_owner"] == f"pid:{os.getpid()}"
    calls = []

    prompt = recover_local_push_prompt(
        session_id="session-1",
        profile="coder",
        workspace=snapshot.repo.workspace,
        store=store,
        now=time.time(),
        deadline=time.monotonic() + 1.0,
        classify_local_main=lambda **kwargs: calls.append(kwargs) or "integration",
    )

    assert prompt is None
    assert calls == []
    assert store.get_plan("bp-local") == before


def test_restart_prompt_hides_ambiguous_invalid_expired_or_terminal_records(
    tmp_path,
):
    from agent.bestplan_local_push import recover_local_push_prompt

    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    width = len(snapshot.head_oid)
    integration_oid = "1" * width
    store = _store(tmp_path)
    _seed_local_plan(store, snapshot, integration_oid)
    _prepare(store, snapshot, integration_oid)
    assert _activate(store, snapshot, integration_oid)
    now = time.time()

    def recover(**overrides):
        values = {
            "session_id": "session-1",
            "profile": "coder",
            "workspace": snapshot.repo.workspace,
            "store": store,
            "now": now,
        }
        values.update(overrides)
        return recover_local_push_prompt(**values)

    assert recover(session_id="no-such-session") is None
    assert recover(profile="other-profile") is None
    assert recover(workspace=str(tmp_path / "other-workspace")) is None
    assert recover(now=now + 700) is None
    assert store.get_plan("bp-local")["local_push_state"] == "awaiting"

    store._execute_write(
        lambda conn: conn.execute(
            "UPDATE bestplan_plans SET state='failed' WHERE plan_id='bp-local'"
        )
    )
    assert recover() is None
    store._execute_write(
        lambda conn: conn.execute(
            "UPDATE bestplan_plans SET state=? WHERE plan_id='bp-local'",
            (PlanState.COMPLETED_LOCAL,),
        )
    )

    original = store.get_plan("bp-local")["local_push_json"]
    store._execute_write(
        lambda conn: conn.execute(
            "UPDATE bestplan_plans SET local_push_json='{}' "
            "WHERE plan_id='bp-local'"
        )
    )
    assert recover() is None
    assert store.get_plan("bp-local")["local_push_json"] == "{}"
    store._execute_write(
        lambda conn: conn.execute(
            "UPDATE bestplan_plans SET local_push_json=? WHERE plan_id='bp-local'",
            (original,),
        )
    )

    for state in (
        "prepared", "pushing", "effect_unknown", "stale", "declined", "pushed",
    ):
        store._execute_write(
            lambda conn, state=state: conn.execute(
                "UPDATE bestplan_plans SET local_push_state=? "
                "WHERE plan_id='bp-local'",
                (state,),
            )
        )
        assert recover() is None
    store._execute_write(
        lambda conn: conn.execute(
            "UPDATE bestplan_plans SET local_push_state='awaiting' "
            "WHERE plan_id='bp-local'"
        )
    )

    other_oid = "2" * width
    _seed_local_plan(
        store,
        snapshot,
        other_oid,
        plan_id="bp-other",
        session_id="session-1",
        profile="coder",
    )
    assert store.prepare_local_push(
        "bp-other",
        session_id="session-1",
        profile="coder",
        workspace=snapshot.repo.workspace,
        expected_target_oid=snapshot.head_oid,
        integration_oid=other_oid,
        check_set_digest="d" * 64,
        target=_target(snapshot, other_oid),
        expires_at=int(now) + 600,
    ) is not None
    assert store.activate_local_push(
        "bp-other",
        landing_receipt=LocalMainLandingReceipt(
            target_ref=LOCAL_MAIN_REF,
            old_oid=snapshot.head_oid,
            new_oid=other_oid,
            check_receipt_digest="d" * 64,
        ),
    )
    assert recover() is None


def test_classic_cli_startup_renders_recovered_prompt_in_exact_context(
    tmp_path, monkeypatch,
):
    import inspect

    import cli as cli_module
    from agent import bestplan_local_push

    expected = (
        "Local `main` is now `1111111111111111111111111111111111111111` "
        "and approved checks passed. Push this exact commit to "
        "`ssh://git.example.invalid/project.git` `refs/heads/main`? "
        "Reply `push` or `no`."
    )
    seen = []

    def recover(**kwargs):
        seen.append(kwargs)
        return expected

    monkeypatch.setattr(bestplan_local_push, "recover_local_push_prompt", recover)
    monkeypatch.setenv("HERMES_PROFILE", "coder")
    monkeypatch.chdir(tmp_path)
    shell = object.__new__(cli_module.HermesCLI)
    shell.session_id = "session-1"
    rendered = []
    shell._console_print = lambda *args, **kwargs: rendered.append((args, kwargs))

    started = time.monotonic()
    cli_module.HermesCLI._show_recovered_local_push_prompt(shell)
    finished = time.monotonic()

    assert len(seen) == 1
    recovery_deadline = seen[0].pop("deadline")
    assert (
        started + bestplan_local_push.LOCAL_PUSH_PROMPT_RECOVERY_SECONDS
        <= recovery_deadline
        <= finished + bestplan_local_push.LOCAL_PUSH_PROMPT_RECOVERY_SECONDS
    )
    assert seen == [{
        "session_id": "session-1",
        "profile": "coder",
        "workspace": str(tmp_path),
    }]
    assert rendered == [((expected,), {"highlight": False, "markup": False})]
    run_source = inspect.getsource(cli_module.HermesCLI.run)
    assert run_source.count(
        "self._show_recovered_local_push_prompt()"
    ) == 1
    assert run_source.index("self._show_recovered_local_push_prompt()") < (
        run_source.index("prewarm_picker_cache_async")
    )

    monkeypatch.setattr(
        bestplan_local_push,
        "recover_local_push_prompt",
        lambda **_kwargs: None,
    )
    cli_module.HermesCLI._show_recovered_local_push_prompt(shell)
    assert len(rendered) == 1
