"""Async tracker recovery for a durable BestPlan review job."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _logical_review_store_clock(monkeypatch):
    """Keep synthetic lease timestamps independent from the host wall clock."""

    from agent.review_engine import ReviewStore

    monkeypatch.setattr(ReviewStore, "_lease_now_ns", lambda _self: 0)


def _target(review):
    return review.ReviewTarget.bestplan_integration(
        plan_id="recoverable-plan",
        generation=0,
        base_oid="1" * 40,
        local_target_oid="2" * 40,
        integration_oid="3" * 40,
        integration_tree_oid="4" * 40,
        integration_ref="refs/hermes-bestplan-integrations/recoverable-plan/0",
        integration_receipt_digest="5" * 64,
        check_receipt_digest="6" * 64,
        approval_digest="7" * 64,
        contract_digest="8" * 64,
        diff_sha256="9" * 64,
        acceptance_digest="a" * 64,
        policy_digest="b" * 64,
    )


def _review_routes(secret: str = ""):
    routes = [
        {
            "route": "smart_reviewer",
            "provider": "anthropic",
            "model": "claude-opus-5",
            "runtime_fingerprint": "e" * 64,
        },
        {
            "route": "code_worker",
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
            "runtime_fingerprint": "f" * 64,
        },
    ]
    if secret:
        for item in routes:
            item["api_key"] = secret
            item["headers"] = {"authorization": f"Bearer {secret}"}
    return routes


def _seed_pre_review_execution(
    tmp_path: Path,
    monkeypatch,
    *,
    candidate_route_overrides: dict[str, object] | None = None,
    corrupt_runtime_routes: object | None = None,
    intent_overrides: dict[str, object] | None = None,
) -> SimpleNamespace:
    """Persist one canonical local plan and one dead pre-review owner."""

    from agent import bestplan_state, review_engine
    from agent.bestplan_contract import source_snapshot_digest
    from agent.bestplan_local import (
        LocalReviewAuthorityBinding,
        local_go_manifest_digest,
    )
    from tests.agent.test_bestplan_local_ingress import _capture_local_plan
    from tools import delegate_tool

    plan_store, snapshot, captured = _capture_local_plan(tmp_path, monkeypatch)
    assert captured.plan_id is not None
    plan_id = captured.plan_id
    candidate_runtime = {
        "route": "code_worker",
        "provider": "openai-codex",
        "model": "gpt-5.6-sol",
        "runtime_fingerprint": "a" * 64,
    }
    prepared = plan_store.prepare_dispatch_intent(
        plan_id,
        snapshot.fingerprint,
        resolved_runtimes=[candidate_runtime],
        session_id="local-session",
        profile="coder",
        workspace=snapshot.repo.workspace,
    )
    assert prepared is not None
    validated = bestplan_state._validate_stored_plan_row(prepared)
    candidate_route = {
        **delegate_tool._bestplan_async_runtime_metadata(
            candidate_runtime,
            candidate_toolsets=("file",),
            execution_protocol=2,
        ),
        "route": "candidate-0",
    }
    candidate_route.update(candidate_route_overrides or {})
    review_bindings = tuple(
        LocalReviewAuthorityBinding(
            slot=route["route"],
            provider=route["provider"],
            model=route["model"],
            model_family=family,
            runtime_fingerprint=route["runtime_fingerprint"],
            authority=object(),
        )
        for route, family in zip(
            _review_routes(), ("claude", "gpt"), strict=True,
        )
    )
    review_routes = delegate_tool._bestplan_sanitized_review_runtime_routes(
        review_bindings
    )
    state_db = Path(plan_store.state_db_path).resolve()
    tracker = (state_db.parent / "async_delegations.json").resolve()
    delegation_id = str(prepared["dispatch_id"])
    job_id = delegate_tool._bestplan_safe_identifier("review-job", plan_id)
    review_store = review_engine.ReviewStore(state_db)
    raw_request = "Implement the exact recovered change"
    adapter_state = {
        "schema": "hermes.bestplan.execution-intent.v1",
        "approval_digest": validated.approval_digest,
        "contract_digest": prepared["promotion_contract_digest"],
        "manifest_digest": local_go_manifest_digest(validated.manifest),
        "raw_request": raw_request,
        "raw_request_sha256": hashlib.sha256(
            raw_request.encode("utf-8")
        ).hexdigest(),
        "source_snapshot_digest": source_snapshot_digest(snapshot),
    }
    adapter_state.update(intent_overrides or {})
    review_store.create_execution_pipeline(
        plan_id=plan_id,
        delegation_id=delegation_id,
        job_id=job_id,
        owner_session_id="local-session",
        owner_profile="coder",
        workspace=snapshot.repo.workspace,
        adapter_state=adapter_state,
        runtime_routes=[candidate_route, *review_routes],
        candidate_count=1,
    )
    if corrupt_runtime_routes is not None:
        with sqlite3.connect(state_db) as connection:
            connection.execute(
                "DROP TRIGGER "
                "trg_bestplan_execution_pipeline_identity_immutable"
            )
            connection.execute(
                "UPDATE bestplan_execution_pipelines SET runtime_routes_json=? "
                "WHERE plan_id=?",
                (
                    json.dumps(corrupt_runtime_routes),
                    plan_id,
                ),
            )
    old_owner_pid = 999_999_999
    old_owner_start = "dead-owner-start"
    assert review_store.allocate_execution_attempt(
        plan_id,
        owner_pid=old_owner_pid,
        owner_process_start_id=old_owner_start,
    ) == 0
    old_attempt_id = delegate_tool._bestplan_execution_attempt_plan_id(plan_id, 0)
    old_root = tmp_path / "attempt-roots" / old_attempt_id
    old_root.mkdir(parents=True)
    old_sentinel = old_root / "old-owner-sentinel"
    old_sentinel.write_bytes(b"do-not-touch")
    record = {
        "delegation_id": delegation_id,
        "goal": "execute recoverable BestPlan",
        "goals": ["Implement the approved change"],
        "session_key": "session-key",
        "origin_session_id": "local-session",
        "origin_profile": "coder",
        "origin_tracker_path": str(tracker),
        "status": "review_requeued",
        "delivery_status": "review_requeued",
        "dispatched_at": time.time(),
        "last_heartbeat_at": time.time(),
        "owner_pid": old_owner_pid,
        "owner_started_at": old_owner_start,
        "bestplan_plan_id": plan_id,
        "bestplan_local_execution": True,
        "bestplan_state_db_path": str(state_db),
        "bestplan_review_job_id": job_id,
        "resolved_runtimes": [candidate_route],
        "is_batch": True,
    }
    tracker.write_text(json.dumps({
        "version": 1,
        "records": {
            delegation_id: {
                "delegation_id": delegation_id,
                "record": record,
                "status": "review_requeued",
                "delivery_status": "review_requeued",
            },
        },
    }), encoding="utf-8")
    request = {
        "adapter_version": "local-bestplan-execution.v1",
        "delegation_id": delegation_id,
        "job_id": job_id,
        "kind": "bestplan_execution_resume",
        "plan_id": plan_id,
        "profile": "coder",
        "session_id": "local-session",
        "state_db_path": str(state_db),
        "tracker_path": str(tracker),
        "workspace": snapshot.repo.workspace,
    }
    return SimpleNamespace(
        candidate_runtime=candidate_runtime,
        old_root=old_root,
        old_sentinel=old_sentinel,
        plan_id=plan_id,
        plan_store=plan_store,
        request=request,
        review_routes=review_routes,
        review_store=review_store,
        snapshot=snapshot,
        state_db=state_db,
        tracker=tracker,
    )


def _seed_recovery_process(
    state_db: str,
    tracker: str,
    workspace: str,
    phase: str,
    secret: str,
) -> None:
    """Create one durable checkpoint in a process that then exits."""

    from agent import review_engine
    from gateway.status import get_process_start_time

    target = _target(review_engine)
    store = review_engine.ReviewStore(state_db)
    # This spawned fixture uses the synthetic 1_000 ns timeline below. Pytest
    # fixtures do not run inside the spawned child, so bind the same logical
    # clock explicitly before creating its durable checkpoints.
    store._lease_now_ns = lambda: 0
    store.create_job(
        job_id="review-job-recovery",
        source_kind=target.source_kind,
        source_id=target.plan_id,
        target_digest=target.target_digest,
        policy_digest=target.policy_digest,
        integration_oid=target.integration_oid,
        check_receipt_digest=target.check_receipt_digest,
        adapter_version="local-bestplan.v1",
        owner_session_id="session-recovery",
        owner_profile="profile-recovery",
        workspace=workspace,
        adapter_state={
            "schema": "hermes.bestplan.local-review-adapter.v1",
            "manifest_digest": "c" * 64,
        },
        runtime_routes=_review_routes(secret),
    )
    claim = store.claim_job(
        job_id="review-job-recovery",
        owner_id="worker-before-crash",
        now_ns=1_000,
        lease_duration_ns=1,
        expected_fencing_token=0,
    )
    store.begin_generation(
        job_id="review-job-recovery",
        generation=0,
        target=target,
        owner_id=claim.owner_id,
        fencing_token=claim.fencing_token,
        operation_id="generation-0",
    )

    def record_slot(slot: str, *, passed: bool) -> None:
        store.record_reviewer_receipt(
            job_id="review-job-recovery",
            generation=0,
            slot=slot,
            target_digest=target.target_digest,
            integration_oid=target.integration_oid,
            output_digest=("1" if slot == "smart_reviewer" else "2") * 64,
            verdict_digest=("3" if slot == "smart_reviewer" else "4") * 64,
            passed=passed,
            receipt_json=json.dumps(
                {
                    "schema": "hermes.bestplan.stored-reviewer-receipt.v1",
                    "slot": slot,
                    "target_digest": target.target_digest,
                    "integration_oid": target.integration_oid,
                    "findings": [] if passed else [{"fingerprint": "5" * 64}],
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            owner_id=claim.owner_id,
            fencing_token=claim.fencing_token,
            operation_id=f"reviewer-0-{slot}",
        )

    record_slot("smart_reviewer", passed=phase == "slot_1")
    if phase != "slot_1":
        record_slot("code_worker", passed=True)
        store.record_generation_blocked(
            job_id="review-job-recovery",
            generation=0,
            target_digest=target.target_digest,
            integration_oid=target.integration_oid,
            check_receipt_digest=target.check_receipt_digest,
            review_receipt_digest="6" * 64,
            blocking_findings_json=json.dumps(
                [{"fingerprint": "5" * 64}],
                sort_keys=True,
                separators=(",", ":"),
            ),
            owner_id=claim.owner_id,
            fencing_token=claim.fencing_token,
            operation_id="blocked-0",
        )
        store.record_repair_frozen(
            job_id="review-job-recovery",
            prior_generation=0,
            generation=1,
            prior_target_digest=target.target_digest,
            integration_oid="4" * 40,
            integration_tree_oid="7" * 40,
            integration_ref=(
                "refs/hermes-bestplan-integrations/recoverable-plan/1"
            ),
            integration_receipt_digest="8" * 64,
            candidate_receipts_json=json.dumps(
                [{"slice_id": "slice-a", "candidate_oid": "9" * 40}],
                sort_keys=True,
                separators=(",", ":"),
            ),
            owner_id=claim.owner_id,
            fencing_token=claim.fencing_token,
            operation_id="repair-frozen-1",
        )
    if phase == "checks_checkpoint":
        next_target = review_engine.ReviewTarget.bestplan_integration(
            plan_id="recoverable-plan",
            generation=1,
            base_oid="1" * 40,
            local_target_oid="2" * 40,
            integration_oid="4" * 40,
            integration_tree_oid="7" * 40,
            integration_ref=(
                "refs/hermes-bestplan-integrations/recoverable-plan/1"
            ),
            integration_receipt_digest="8" * 64,
            check_receipt_digest="9" * 64,
            approval_digest="7" * 64,
            contract_digest="8" * 64,
            diff_sha256="a" * 64,
            acceptance_digest="a" * 64,
            policy_digest="b" * 64,
        )
        store.record_checks_passed(
            job_id="review-job-recovery",
            generation=1,
            target=next_target,
            check_receipt_json=json.dumps(
                {
                    "schema": "hermes.bestplan.check-receipt.v1",
                    "integration_oid": next_target.integration_oid,
                    "receipt_digest": next_target.check_receipt_digest,
                    "status": "passed",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            owner_id=claim.owner_id,
            fencing_token=claim.fencing_token,
            operation_id="checks-passed-1",
        )

    now = time.time()
    record = {
        "delegation_id": f"delegation-{phase}",
        "goal": "execute recoverable BestPlan",
        "goals": ["implement slice-a"],
        "session_key": "session-key",
        "origin_session_id": "session-recovery",
        "origin_profile": "profile-recovery",
        "origin_tracker_path": tracker,
        "status": "running",
        "delivery_status": "running",
        "dispatched_at": now,
        "last_heartbeat_at": now,
        "owner_pid": os.getpid(),
        "owner_started_at": get_process_start_time(os.getpid()),
        "bestplan_plan_id": target.plan_id,
        "bestplan_local_execution": True,
        "bestplan_state_db_path": state_db,
        "bestplan_review_job_id": "review-job-recovery",
        "resolved_runtimes": [
            {"route": item["route"], "runtime_fingerprint": item["runtime_fingerprint"]}
            for item in _review_routes(secret)
        ],
        "is_batch": True,
    }
    with open(tracker, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "version": 1,
                "records": {
                    record["delegation_id"]: {
                        "delegation_id": record["delegation_id"],
                        "record": record,
                        "status": "running",
                        "delivery_status": "running",
                    }
                },
            },
            handle,
        )


class _LiveRecoveryAdapter:
    def __init__(self, *, fingerprint_suffix: str = ""):
        self.fingerprint_suffix = fingerprint_suffix
        self.actions = []
        self.git_repairs = 0

    def resolve_runtime_routes(self, *, job, request):
        del job, request
        routes = _review_routes("live-provider-secret")
        if self.fingerprint_suffix:
            routes[1]["runtime_fingerprint"] = self.fingerprint_suffix * 64
        return routes

    def start_generation(self, **kwargs):
        self.actions.append(("start_generation", kwargs["resume"]))
        return {"status": "checkpoint_adopted"}

    def review_missing_slots(self, **kwargs):
        self.actions.append(("review_missing_slots", kwargs["resume"]))
        return {"status": "checkpoint_adopted"}

    def repair(self, **kwargs):
        self.git_repairs += 1
        self.actions.append(("repair", kwargs["resume"]))
        return {"status": "checkpoint_adopted"}

    def checks(self, **kwargs):
        self.actions.append(("checks", kwargs["resume"]))
        return {"status": "checkpoint_adopted"}

    def handoff_pass(self, **kwargs):
        self.actions.append(("handoff_pass", kwargs["resume"]))
        return {"status": "checkpoint_adopted"}

    def wait_for_host(self, **kwargs):
        self.actions.append(("wait_for_host", kwargs["resume"]))
        return {"status": "checkpoint_adopted"}


def test_dead_bestplan_runner_requeues_its_durable_review_instead_of_marking_lost(
    tmp_path,
):
    from agent import review_engine
    from tools import async_delegation as async_delegation

    tracker = tmp_path / "async-delegations.json"
    state_db = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = "provider-secret-must-not-reach-recovery-state"
    target = _target(review_engine)
    store = review_engine.ReviewStore(state_db)
    store.create_job(
        job_id="review-job-recovery",
        source_kind=target.source_kind,
        source_id=target.plan_id,
        target_digest=target.target_digest,
        policy_digest=target.policy_digest,
        integration_oid=target.integration_oid,
        check_receipt_digest=target.check_receipt_digest,
        adapter_version="local-bestplan.v1",
        owner_session_id="session-recovery",
        owner_profile="profile-recovery",
        workspace=str(workspace),
        adapter_state={
            "schema": "hermes.bestplan.local-review-adapter.v1",
            "manifest_digest": "c" * 64,
            "snapshot_receipt_digest": "d" * 64,
        },
        runtime_routes=[
            {
                "route": "smart_reviewer",
                "provider": "anthropic",
                "model": "claude-opus-5",
                "runtime_fingerprint": "e" * 64,
                "api_key": secret,
            },
            {
                "route": "code_worker",
                "provider": "openai-codex",
                "model": "gpt-5.6-sol",
                "runtime_fingerprint": "f" * 64,
                "api_key": secret,
            },
        ],
    )
    now = time.time()
    record = {
        "delegation_id": "delegation-review-recovery",
        "goal": "execute recoverable BestPlan",
        "goals": ["implement slice-a"],
        "session_key": "session-key",
        "origin_session_id": "session-recovery",
        "origin_profile": "profile-recovery",
        "status": "running",
        "delivery_status": "running",
        "dispatched_at": now,
        "last_heartbeat_at": now,
        "owner_pid": 999_999_999,
        "owner_started_at": 1.0,
        "bestplan_plan_id": target.plan_id,
        "bestplan_local_execution": True,
        "bestplan_state_db_path": str(state_db),
        "bestplan_review_job_id": "review-job-recovery",
        "resolved_runtimes": [
            {
                "route": "smart_reviewer",
                "runtime_fingerprint": "e" * 64,
            },
            {
                "route": "code_worker",
                "runtime_fingerprint": "f" * 64,
            },
        ],
    }
    tracker.write_text(
        json.dumps(
            {
                "version": 1,
                "records": {
                    record["delegation_id"]: {
                        "delegation_id": record["delegation_id"],
                        "record": record,
                        "status": "running",
                        "delivery_status": "running",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    completion_queue: queue.Queue = queue.Queue()
    review_recovery_queue: queue.Queue = queue.Queue()

    result = async_delegation.recover_async_delegations(
        tracker,
        target_queue=completion_queue,
        review_recovery_queue=review_recovery_queue,
    )

    assert result["lost"] == 0
    assert result["review_requeued"] == 1
    assert completion_queue.empty()
    request = review_recovery_queue.get_nowait()
    assert request == {
        "kind": "bestplan_review_resume",
        "delegation_id": "delegation-review-recovery",
        "job_id": "review-job-recovery",
        "plan_id": target.plan_id,
        "state_db_path": str(state_db.resolve()),
        "tracker_path": str(tracker.resolve()),
        "adapter_version": "local-bestplan.v1",
        "session_id": "session-recovery",
        "profile": "profile-recovery",
        "workspace": str(workspace.resolve()),
    }
    persisted = json.loads(tracker.read_text(encoding="utf-8"))
    entry = persisted["records"][record["delegation_id"]]
    assert entry["status"] != "lost"
    assert entry.get("event") is None
    assert secret.encode("utf-8") not in state_db.read_bytes()
    assert secret not in tracker.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("phase", "expected_action", "missing_slots"),
    [
        ("slot_1", "review_missing_slots", ("code_worker",)),
        ("repair_freeze", "checks", ()),
        (
            "checks_checkpoint",
            "review_missing_slots",
            ("smart_reviewer", "code_worker"),
        ),
    ],
)
def test_new_process_store_worker_adopts_exact_review_checkpoint_without_git_replay(
    tmp_path,
    phase,
    expected_action,
    missing_slots,
):
    from tools import async_delegation
    from tools import delegate_tool

    state_db = (tmp_path / f"{phase}.db").resolve()
    tracker = (tmp_path / f"{phase}.json").resolve()
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir(exist_ok=True)
    secret = "child-provider-secret-must-not-persist"
    process = multiprocessing.get_context("spawn").Process(
        target=_seed_recovery_process,
        args=(str(state_db), str(tracker), str(workspace), phase, secret),
    )
    process.start()
    process.join(timeout=20)
    assert process.exitcode == 0

    recovery_queue: queue.Queue = queue.Queue()
    recovered = async_delegation.recover_async_delegations(
        tracker,
        target_queue=queue.Queue(),
        review_recovery_queue=recovery_queue,
    )
    assert recovered["lost"] == 0
    assert recovered["review_requeued"] == 1

    adapter = _LiveRecoveryAdapter()
    consumed = async_delegation.consume_bestplan_review_recoveries(
        recovery_queue,
        worker=lambda request: delegate_tool.resume_bestplan_review_request(
            request,
            adapter=adapter,
            now_ns=time.time_ns() + 1_000_000_000,
        ),
        max_items=1,
    )

    assert consumed == {"consumed": 1, "completed": 0, "deferred": 0}
    assert [item[0] for item in adapter.actions] == [expected_action]
    assert adapter.actions[0][1].missing_reviewer_slots == missing_slots
    assert adapter.git_repairs == 0
    persisted = json.loads(tracker.read_text(encoding="utf-8"))
    entry = persisted["records"][f"delegation-{phase}"]
    assert entry["status"] != "lost"
    assert entry.get("event") is None
    assert secret not in state_db.read_bytes().decode("utf-8", "ignore")
    assert secret not in tracker.read_text(encoding="utf-8")


def test_recovery_worker_rejects_changed_live_runtime_before_claim_or_side_effect(
    tmp_path,
):
    from agent import review_engine
    from tools import async_delegation
    from tools import delegate_tool

    state_db = (tmp_path / "stale.db").resolve()
    tracker = (tmp_path / "stale.json").resolve()
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    process = multiprocessing.get_context("spawn").Process(
        target=_seed_recovery_process,
        args=(
            str(state_db),
            str(tracker),
            str(workspace),
            "slot_1",
            "child-provider-secret-must-not-persist",
        ),
    )
    process.start()
    process.join(timeout=20)
    assert process.exitcode == 0
    before = review_engine.ReviewStore(state_db).get_job("review-job-recovery")
    recovery_queue: queue.Queue = queue.Queue()
    async_delegation.recover_async_delegations(
        tracker,
        target_queue=queue.Queue(),
        review_recovery_queue=recovery_queue,
    )
    adapter = _LiveRecoveryAdapter(fingerprint_suffix="0")

    consumed = async_delegation.consume_bestplan_review_recoveries(
        recovery_queue,
        worker=lambda request: delegate_tool.resume_bestplan_review_request(
            request,
            adapter=adapter,
            now_ns=time.time_ns() + 1_000_000_000,
        ),
        max_items=1,
    )

    assert consumed == {"consumed": 1, "completed": 0, "deferred": 1}
    after = review_engine.ReviewStore(state_db).get_job("review-job-recovery")
    assert after.fencing_token == before.fencing_token
    assert adapter.actions == []
    assert adapter.git_repairs == 0
    persisted = json.loads(tracker.read_text(encoding="utf-8"))
    assert persisted["records"]["delegation-slot_1"]["status"] == "review_waiting"


def test_local_bestplan_dispatch_hands_deterministic_review_job_id_to_tracker(
    tmp_path,
    monkeypatch,
):
    from agent import bestplan_local
    from tests.agent.test_bestplan_local_flow import _review_loop_inputs
    from tools import delegate_tool

    admitted = {}
    inputs = _review_loop_inputs(tmp_path)
    workspace = inputs.snapshot.repo.workspace
    spec = SimpleNamespace(toolsets=("file",))
    monkeypatch.setattr(
        delegate_tool,
        "_preflight_bestplan_candidates",
        lambda **_kwargs: [{
            "position": 0,
            "spec": spec,
            "attempt_id": "attempt-a",
            "manifest_slice_id": "slice-a",
        }],
    )
    monkeypatch.setattr(
        delegate_tool,
        "_ordered_bestplan_authority_clients",
        lambda *_args, **_kwargs: (object(),),
    )
    monkeypatch.setattr(
        delegate_tool,
        "_bestplan_async_runtime_metadata",
        lambda runtime, **_kwargs: dict(runtime),
    )
    monkeypatch.setattr(
        "gateway.session_context.get_delivery_context_identity",
        lambda: {
            "capability_version": 1,
            "session_key": "session-key",
            "session_id": "session-recovery",
            "ui_session_id": "ui-session",
            "profile": "profile-recovery",
            "tracker_path": str((tmp_path / "tracker.json").resolve()),
        },
    )
    monkeypatch.setattr(
        "tools.async_delegation.dispatch_async_delegation_batch",
        lambda **kwargs: admitted.update(kwargs) or {
            "status": "dispatched",
            "delegation_id": kwargs["delegation_id"],
        },
    )
    plan_id = "recoverable-plan"

    result = delegate_tool.dispatch_bestplan_tasks_async(
        tasks=[{
            "goal": "repair slice",
            "route": "code_worker",
            "_bestplan_read_only": False,
        }],
        parent_agent=SimpleNamespace(),
        dispatch_id="delegation-recovery",
        plan_id=plan_id,
        workspace=str(workspace),
        resolved_runtimes=[{
            "route": "code_worker",
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
            "runtime_fingerprint": "a" * 64,
        }],
        execution_protocol=2,
        source_snapshot=inputs.snapshot,
        approval_digest="b" * 64,
        promotion_contract={"schema": "hermes.bestplan.local-go.v1"},
        promotion_contract_digest="c" * 64,
        promotion_mode="local_main",
        execution_plan=inputs.plan,
        local_execution_runtime=inputs.runtime,
        candidate_host_runtime=object(),
        authority_bindings=(object(),),
        review_authority_bindings=tuple(
            bestplan_local.LocalReviewAuthorityBinding(
                slot=route["route"],
                provider=route["provider"],
                model=route["model"],
                model_family=family,
                runtime_fingerprint=route["runtime_fingerprint"],
                authority=object(),
            )
            for route, family in zip(
                _review_routes(), ("claude", "gpt"), strict=True
            )
        ),
        raw_request="repair slice",
        state_db_path=(tmp_path / "state.db").resolve(),
    )

    assert result["status"] == "dispatched"
    assert admitted["origin_session_id"] == "session-recovery"
    assert admitted["bestplan_review_job_id"] == (
        delegate_tool._bestplan_safe_identifier("review-job", plan_id)
    )


def test_restart_intent_rejects_untyped_review_route_binding():
    from tools import delegate_tool

    fake = SimpleNamespace(
        slot="smart_reviewer",
        provider="anthropic",
        model="claude-opus-5",
        model_family="claude",
        runtime_fingerprint="a" * 64,
        authority=object(),
    )
    with pytest.raises(ValueError, match="review authority binding"):
        delegate_tool._bestplan_sanitized_review_runtime_routes((fake, fake))


def test_pre_review_restart_allocates_a_fresh_attempt_namespace(
    tmp_path,
    monkeypatch,
):
    """A dead pre-review owner must not reuse any first-attempt identity."""

    from agent import bestplan_candidates, bestplan_local, bestplan_state
    from agent.review_engine import ReviewStore
    from tests.agent.test_bestplan_local_flow import _review_loop_inputs
    from tools import delegate_tool

    inputs = _review_loop_inputs(tmp_path)
    workspace = inputs.snapshot.repo.workspace
    tasks = bestplan_state._plan_to_delegate_tasks(
        inputs.plan, workspace=workspace,
    )
    resolved_runtimes = [
        {
            "route": task["route"],
            "provider": "openai-codex",
            "model": f"gpt-5.6-sol-{index}",
            "runtime_fingerprint": f"{index + 1:x}" * 64,
        }
        for index, task in enumerate(tasks)
    ]
    review_bindings = tuple(
        bestplan_local.LocalReviewAuthorityBinding(
            slot=route["route"],
            provider=route["provider"],
            model=route["model"],
            model_family=family,
            runtime_fingerprint=route["runtime_fingerprint"],
            authority=object(),
        )
        for route, family in zip(
            _review_routes(), ("claude", "gpt"), strict=True,
        )
    )
    tracker = (tmp_path / "tracker.json").resolve()
    state_db = (tmp_path / "state.db").resolve()
    admitted = {}
    built_runtime_plan_ids = []
    candidate_calls = []
    finished = []

    monkeypatch.setattr(
        delegate_tool, "_validate_bestplan_host_runtime", lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        delegate_tool,
        "_ordered_bestplan_authority_clients",
        lambda *_a, **_k: tuple(object() for _task in tasks),
    )
    monkeypatch.setattr(
        "gateway.session_context.get_delivery_context_identity",
        lambda: {
            "capability_version": 1,
            "session_key": "session-key",
            "session_id": "session-recovery",
            "ui_session_id": "ui-session",
            "profile": "profile-recovery",
            "tracker_path": str(tracker),
        },
    )
    monkeypatch.setattr(
        "tools.async_delegation.dispatch_async_delegation_batch",
        lambda **kwargs: admitted.update(kwargs) or {
            "status": "dispatched",
            "delegation_id": kwargs["delegation_id"],
        },
    )

    def build_attempt_runtime(**kwargs):
        attempt_plan_id = kwargs["plan_id"]
        built_runtime_plan_ids.append(attempt_plan_id)
        candidate_runtime = SimpleNamespace(
            **{
                **vars(inputs.runtime.candidate_runtime),
                "attempts_root": tmp_path / "attempt-roots" / attempt_plan_id,
            }
        )
        return SimpleNamespace(
            **{
                **vars(inputs.runtime),
                "candidate_runtime": candidate_runtime,
                "integration_root": tmp_path / "integration-roots" / attempt_plan_id,
                "checks_root": tmp_path / "check-roots" / attempt_plan_id,
            }
        )

    monkeypatch.setattr(
        bestplan_local, "build_local_execution_runtime", build_attempt_runtime,
    )

    def run_candidate(*, spec, attempt_id, attempts_root, **_kwargs):
        candidate_calls.append({
            "attempt_id": attempt_id,
            "attempts_root": str(attempts_root),
            "candidate_id": spec.candidate_id,
            "plan_id": spec.plan_id,
            "ref_name": bestplan_candidates.candidate_ref_name(
                spec.plan_id, spec.slice_id, attempt_id,
            ),
            "slice_id": spec.slice_id,
        })
        return SimpleNamespace(
            candidate_id=spec.candidate_id,
            slice_id=spec.slice_id,
            attempt_id=attempt_id,
        )

    monkeypatch.setattr(
        bestplan_candidates, "run_and_freeze_candidate", run_candidate,
    )
    monkeypatch.setattr(
        delegate_tool, "_validate_bestplan_frozen_candidate", lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        delegate_tool,
        "_bestplan_candidate_projection",
        lambda **kwargs: {
            "candidate_id": kwargs["spec"].candidate_id,
            "attempt_id": kwargs["spec"].plan_id,
        },
    )
    monkeypatch.setattr(
        delegate_tool,
        "_finish_local_bestplan_batch",
        lambda **kwargs: finished.append(kwargs) or {"status": "review_started"},
    )

    plan_id = "recoverable-plan"
    result = delegate_tool.dispatch_bestplan_tasks_async(
        tasks=tasks,
        parent_agent=SimpleNamespace(),
        dispatch_id="delegation-recovery",
        plan_id=plan_id,
        workspace=workspace,
        resolved_runtimes=resolved_runtimes,
        execution_protocol=2,
        source_snapshot=inputs.snapshot,
        approval_digest="b" * 64,
        promotion_contract={"schema": "hermes.bestplan.local-go.v1"},
        promotion_contract_digest="c" * 64,
        promotion_mode="local_main",
        execution_plan=inputs.plan,
        local_execution_runtime=inputs.runtime,
        candidate_host_runtime=inputs.runtime.candidate_runtime,
        authority_bindings=tuple(object() for _task in tasks),
        review_authority_bindings=review_bindings,
        raw_request="repair both slices",
        state_db_path=state_db,
    )
    assert result["status"] == "dispatched"

    # Simulate a consumed ordinal from a prior process. The later recovery
    # claim must never reuse it.
    store = ReviewStore(state_db)
    with sqlite3.connect(state_db) as connection:
        connection.execute(
            "UPDATE bestplan_execution_pipelines "
            "SET next_attempt_ordinal=1 WHERE plan_id=?",
            (plan_id,),
        )

    assert admitted["runner"]() == {"status": "review_started"}
    second_attempt_plan_id = delegate_tool._bestplan_execution_attempt_plan_id(
        plan_id, 1,
    )
    assert built_runtime_plan_ids == [second_attempt_plan_id]
    assert {item["plan_id"] for item in candidate_calls} == {
        second_attempt_plan_id,
    }
    assert all(second_attempt_plan_id in item["attempts_root"] for item in candidate_calls)
    assert all(second_attempt_plan_id in item["ref_name"] for item in candidate_calls)
    assert finished[0]["plan_id"] == plan_id
    assert finished[0]["identity_plan_id"] == second_attempt_plan_id
    assert store.get_execution_pipeline(plan_id).next_attempt_ordinal == 2


@pytest.mark.parametrize(
    "failure_phase",
    ("runtime_build", "runtime_validation", "preflight", "batch"),
)
def test_initial_pre_review_failure_releases_exact_attempt_owner(
    tmp_path,
    monkeypatch,
    failure_phase,
):
    """Every post-allocation failure must leave the attempt recoverable."""

    from agent import bestplan_local
    from agent.review_engine import ReviewStore
    from tests.agent.test_bestplan_local_flow import _review_loop_inputs
    from tools import delegate_tool

    inputs = _review_loop_inputs(tmp_path)
    workspace = inputs.snapshot.repo.workspace
    state_db = (tmp_path / "state.db").resolve()
    tracker = (tmp_path / "tracker.json").resolve()
    admitted: dict[str, object] = {}
    preflight_calls = 0
    prepared = [{
        "position": 0,
        "spec": SimpleNamespace(toolsets=("file",)),
        "attempt_id": "attempt-a",
        "manifest_slice_id": "slice-a",
    }]

    def preflight(**_kwargs):
        nonlocal preflight_calls
        preflight_calls += 1
        if preflight_calls == 2 and failure_phase == "preflight":
            raise RuntimeError("post-allocation preflight failed")
        return prepared

    monkeypatch.setattr(
        delegate_tool, "_preflight_bestplan_candidates", preflight,
    )
    monkeypatch.setattr(
        delegate_tool,
        "_ordered_bestplan_authority_clients",
        lambda *_args, **_kwargs: (object(),),
    )
    monkeypatch.setattr(
        delegate_tool,
        "_bestplan_async_runtime_metadata",
        lambda runtime, **_kwargs: dict(runtime),
    )
    monkeypatch.setattr(
        "gateway.session_context.get_delivery_context_identity",
        lambda: {
            "capability_version": 1,
            "session_key": "session-key",
            "session_id": "session-recovery",
            "ui_session_id": "ui-session",
            "profile": "profile-recovery",
            "tracker_path": str(tracker),
        },
    )
    monkeypatch.setattr(
        "tools.async_delegation.dispatch_async_delegation_batch",
        lambda **kwargs: admitted.update(kwargs) or {
            "status": "dispatched",
            "delegation_id": kwargs["delegation_id"],
        },
    )

    def build_runtime(**_kwargs):
        if failure_phase == "runtime_build":
            raise RuntimeError("post-allocation runtime build failed")
        return SimpleNamespace(candidate_runtime=object())

    monkeypatch.setattr(
        bestplan_local, "build_local_execution_runtime", build_runtime,
    )

    def validate_runtime(*_args, **_kwargs):
        if failure_phase == "runtime_validation":
            raise RuntimeError("post-allocation runtime validation failed")

    monkeypatch.setattr(
        delegate_tool, "_validate_bestplan_host_runtime", validate_runtime,
    )

    def execute_batch(**_kwargs):
        if failure_phase == "batch":
            raise RuntimeError("post-allocation batch failed")
        return {"status": "review_started"}

    monkeypatch.setattr(
        delegate_tool, "_execute_bestplan_candidate_batch", execute_batch,
    )
    review_bindings = tuple(
        bestplan_local.LocalReviewAuthorityBinding(
            slot=route["route"],
            provider=route["provider"],
            model=route["model"],
            model_family=family,
            runtime_fingerprint=route["runtime_fingerprint"],
            authority=object(),
        )
        for route, family in zip(
            _review_routes(), ("claude", "gpt"), strict=True,
        )
    )
    plan_id = "recoverable-plan"

    result = delegate_tool.dispatch_bestplan_tasks_async(
        tasks=[{
            "goal": "repair slice",
            "route": "code_worker",
            "_bestplan_read_only": False,
        }],
        parent_agent=SimpleNamespace(),
        dispatch_id="delegation-recovery",
        plan_id=plan_id,
        workspace=str(workspace),
        resolved_runtimes=[{
            "route": "code_worker",
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
            "runtime_fingerprint": "a" * 64,
        }],
        execution_protocol=2,
        source_snapshot=inputs.snapshot,
        approval_digest="b" * 64,
        promotion_contract={"schema": "hermes.bestplan.local-go.v1"},
        promotion_contract_digest="c" * 64,
        promotion_mode="local_main",
        execution_plan=inputs.plan,
        local_execution_runtime=inputs.runtime,
        candidate_host_runtime=object(),
        authority_bindings=(object(),),
        review_authority_bindings=review_bindings,
        raw_request="repair slice",
        state_db_path=state_db,
    )

    assert result["status"] == "dispatched"
    with pytest.raises(RuntimeError, match="post-allocation"):
        admitted["runner"]()

    pipeline = ReviewStore(state_db).get_execution_pipeline(plan_id)
    assert pipeline.state == "pending"
    assert pipeline.next_attempt_ordinal == 1
    assert pipeline.active_attempt_ordinal is None
    assert pipeline.attempt_owner_pid is None
    assert pipeline.attempt_owner_process_start_id is None


def test_execution_attempt_owner_fence_migrates_and_allows_one_dead_takeover(
    tmp_path,
):
    from agent import review_engine

    state_db = (tmp_path / "old-pipeline.db").resolve()
    with sqlite3.connect(state_db) as connection:
        connection.execute(
            """
            CREATE TABLE bestplan_execution_pipelines (
                plan_id TEXT PRIMARY KEY,
                delegation_id TEXT NOT NULL UNIQUE,
                job_id TEXT NOT NULL UNIQUE,
                owner_session_id TEXT NOT NULL,
                owner_profile TEXT NOT NULL,
                workspace TEXT NOT NULL,
                adapter_version TEXT NOT NULL,
                adapter_state_json TEXT NOT NULL,
                runtime_routes_json TEXT NOT NULL,
                candidate_count INTEGER NOT NULL,
                state TEXT NOT NULL,
                next_attempt_ordinal INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            INSERT INTO bestplan_execution_pipelines VALUES (
                'plan-a', 'delegation-a', 'job-a', 'session-a', 'profile-a',
                ?, 'local-bestplan-execution.v1', '{}', '[]', 1, 'pending', 0
            )
            """,
            (str(tmp_path.resolve()),),
        )

    store = review_engine.ReviewStore(state_db)
    assert store.allocate_execution_attempt(
        "plan-a",
        owner_pid=111,
        owner_process_start_id="kernel-start:111",
    ) == 0
    with pytest.raises(review_engine.ReviewStoreConflict):
        store.allocate_execution_attempt(
            "plan-a",
            owner_pid=222,
            owner_process_start_id="kernel-start:222",
        )
    assert store.get_execution_pipeline("plan-a").next_attempt_ordinal == 1

    assert store.allocate_execution_attempt(
        "plan-a",
        owner_pid=222,
        owner_process_start_id="kernel-start:222",
        expected_owner_pid=111,
        expected_owner_process_start_id="kernel-start:111",
    ) == 1
    claimed = store.get_execution_pipeline("plan-a")
    assert claimed.active_attempt_ordinal == 1
    assert claimed.attempt_owner_pid == 222
    assert claimed.attempt_owner_process_start_id == "kernel-start:222"
    assert claimed.next_attempt_ordinal == 2


def test_dead_pre_review_owner_routes_to_execution_worker_and_new_ordinal(
    tmp_path,
    monkeypatch,
):
    from agent import review_engine
    from tools import async_delegation, delegate_tool

    tracker = (tmp_path / "tracker.json").resolve()
    state_db = (tmp_path / "state.db").resolve()
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    plan_id = "recoverable-plan"
    job_id = delegate_tool._bestplan_safe_identifier("review-job", plan_id)
    store = review_engine.ReviewStore(state_db)
    store.create_execution_pipeline(
        plan_id=plan_id,
        delegation_id="delegation-recovery",
        job_id=job_id,
        owner_session_id="session-recovery",
        owner_profile="profile-recovery",
        workspace=str(workspace),
        adapter_state={
            "schema": "hermes.bestplan.execution-intent.v1",
        },
        runtime_routes=[],
        candidate_count=1,
    )
    store.allocate_execution_attempt(
        plan_id,
        owner_pid=999_999_999,
        owner_process_start_id="dead-owner-start",
    )
    now = time.time()
    record = {
        "delegation_id": "delegation-recovery",
        "goal": "execute recoverable BestPlan",
        "goals": ["implement slice-a"],
        "session_key": "session-key",
        "origin_session_id": "session-recovery",
        "origin_profile": "profile-recovery",
        "origin_tracker_path": str(tracker),
        "status": "running",
        "delivery_status": "running",
        "dispatched_at": now,
        "last_heartbeat_at": now,
        "owner_pid": 999_999_999,
        "owner_started_at": 1,
        "bestplan_plan_id": plan_id,
        "bestplan_local_execution": True,
        "bestplan_state_db_path": str(state_db),
        "bestplan_review_job_id": job_id,
        "resolved_runtimes": [],
        "is_batch": True,
    }
    tracker.write_text(json.dumps({
        "version": 1,
        "records": {
            record["delegation_id"]: {
                "delegation_id": record["delegation_id"],
                "record": record,
                "status": "running",
                "delivery_status": "running",
            },
        },
    }), encoding="utf-8")
    recovery_queue: queue.Queue = queue.Queue()

    recovered = async_delegation.recover_async_delegations(
        tracker,
        target_queue=queue.Queue(),
        review_recovery_queue=recovery_queue,
    )
    assert recovered == {"queued": 0, "lost": 0, "review_requeued": 1}
    request = recovery_queue.get_nowait()
    assert request["kind"] == "bestplan_execution_resume"
    recovery_queue.put(request)
    seen = []
    monkeypatch.setattr(
        delegate_tool,
        "resume_bestplan_execution_request",
        lambda request, **_kwargs: seen.append(dict(request)) or {
            "status": "resumed",
            "result": {"status": "checkpoint_advanced"},
        },
        raising=False,
    )
    monkeypatch.setattr(
        async_delegation,
        "_profile_home_for_bestplan_recovery",
        lambda _request: tmp_path,
        raising=False,
    )

    consumed = async_delegation.consume_bestplan_review_recoveries(
        recovery_queue,
        worker=async_delegation._default_bestplan_review_recovery_worker,
        max_items=1,
    )

    assert consumed == {"consumed": 1, "completed": 0, "deferred": 0}
    assert seen == [request]


def _install_pre_review_recovery_runtime(
    seeded: SimpleNamespace,
    monkeypatch,
    *,
    candidate_fingerprint: str = "a" * 64,
    reviewer_fingerprint: str | None = None,
) -> dict[str, object]:
    """Install live authority leaves while keeping recovery orchestration real."""

    from agent import bestplan_local
    from tools import delegate_tool

    observed: dict[str, object] = {
        "batches": [],
        "preflights": [],
        "runtime_plan_ids": [],
    }
    candidate_runtime = {
        **seeded.candidate_runtime,
        "runtime_fingerprint": candidate_fingerprint,
    }
    review_runtimes = _review_routes()
    if reviewer_fingerprint is not None:
        review_runtimes[0]["runtime_fingerprint"] = reviewer_fingerprint

    def resolve(tasks, _parent, **_kwargs):
        if len(tasks) == 2:
            return [dict(item) for item in review_runtimes]
        return [dict(candidate_runtime)]

    monkeypatch.setattr(
        delegate_tool, "resolve_bestplan_runtime_specs", resolve,
    )

    def build_runtime(**kwargs):
        identity = str(kwargs["plan_id"])
        observed["runtime_plan_ids"].append(identity)
        root = seeded.old_root.parent / identity
        root.mkdir(parents=True)
        (root / "new-owner-sentinel").write_bytes(b"new-attempt")
        return SimpleNamespace(candidate_runtime=object())

    monkeypatch.setattr(
        bestplan_local, "build_local_execution_runtime", build_runtime,
    )
    monkeypatch.setattr(
        delegate_tool, "_validate_bestplan_host_runtime", lambda *_a, **_k: None,
    )

    def preflight(**kwargs):
        observed["preflights"].append(dict(kwargs))
        return ["prepared"]

    monkeypatch.setattr(
        delegate_tool, "_preflight_bestplan_candidates", preflight,
    )

    def execute(**kwargs):
        observed["batches"].append(dict(kwargs))
        return {"status": "review_started"}

    monkeypatch.setattr(
        delegate_tool, "_execute_bestplan_candidate_batch", execute,
    )
    return observed


def test_dead_pre_review_owner_reconstructs_and_runs_fresh_exact_attempt(
    tmp_path,
    monkeypatch,
):
    from gateway.status import get_process_start_time
    from tools import delegate_tool

    seeded = _seed_pre_review_execution(tmp_path, monkeypatch)
    old_inode = seeded.old_sentinel.stat().st_ino
    observed = _install_pre_review_recovery_runtime(seeded, monkeypatch)

    result = delegate_tool.resume_bestplan_execution_request(seeded.request)

    attempt_id = delegate_tool._bestplan_execution_attempt_plan_id(
        seeded.plan_id, 1,
    )
    assert result == {
        "status": "completed",
        "completion": {"status": "review_started"},
    }
    assert observed["runtime_plan_ids"] == [attempt_id]
    assert observed["preflights"][0]["plan_id"] == seeded.plan_id
    assert observed["preflights"][0]["identity_plan_id"] == attempt_id
    assert observed["batches"][0]["plan_id"] == seeded.plan_id
    assert observed["batches"][0]["identity_plan_id"] == attempt_id
    pipeline = seeded.review_store.get_execution_pipeline(seeded.plan_id)
    assert pipeline.next_attempt_ordinal == 2
    assert pipeline.active_attempt_ordinal == 1
    assert pipeline.attempt_owner_pid == os.getpid()
    assert pipeline.attempt_owner_process_start_id == str(
        get_process_start_time(os.getpid())
    )
    assert seeded.old_sentinel.read_bytes() == b"do-not-touch"
    assert seeded.old_sentinel.stat().st_ino == old_inode
    assert (seeded.old_root.parent / attempt_id / "new-owner-sentinel").read_bytes() == (
        b"new-attempt"
    )


def test_two_successive_dead_pipeline_owners_take_fresh_ordinals(
    tmp_path,
    monkeypatch,
):
    """The pipeline fence, not a stale tracker owner, authorizes takeover."""

    from gateway import status as gateway_status
    from tools import delegate_tool

    seeded = _seed_pre_review_execution(tmp_path, monkeypatch)
    observed = _install_pre_review_recovery_runtime(seeded, monkeypatch)
    owner_starts = iter(("recovery-owner-b", "recovery-owner-c"))
    monkeypatch.setattr(gateway_status, "_pid_exists", lambda _pid: False)
    monkeypatch.setattr(
        gateway_status,
        "get_process_start_time",
        lambda _pid: next(owner_starts),
    )

    first = delegate_tool.resume_bestplan_execution_request(seeded.request)
    second = delegate_tool.resume_bestplan_execution_request(seeded.request)

    assert first["status"] == second["status"] == "completed"
    assert observed["runtime_plan_ids"] == [
        delegate_tool._bestplan_execution_attempt_plan_id(seeded.plan_id, 1),
        delegate_tool._bestplan_execution_attempt_plan_id(seeded.plan_id, 2),
    ]
    tracker_payload = json.loads(seeded.tracker.read_text(encoding="utf-8"))
    tracker_record = tracker_payload["records"][
        seeded.request["delegation_id"]
    ]["record"]
    assert tracker_record["owner_pid"] == 999_999_999
    assert tracker_record["owner_started_at"] == "dead-owner-start"
    pipeline = seeded.review_store.get_execution_pipeline(seeded.plan_id)
    assert pipeline.next_attempt_ordinal == 3
    assert pipeline.active_attempt_ordinal == 2
    assert pipeline.attempt_owner_pid == os.getpid()
    assert pipeline.attempt_owner_process_start_id == "recovery-owner-c"


@pytest.mark.parametrize(
    ("drift", "expected_code"),
    [
        ("source", "execution_source_drift"),
        ("contract", "execution_intent_invalid"),
        ("raw_request", "execution_intent_invalid"),
        ("workspace", "execution_intent_invalid"),
        ("candidate_route", "execution_runtime_drift"),
        ("reviewer_route", "execution_runtime_drift"),
    ],
)
def test_pre_review_recovery_drift_has_zero_execution_side_effects(
    tmp_path,
    monkeypatch,
    drift,
    expected_code,
):
    from tools import delegate_tool

    intent_overrides = None
    if drift == "contract":
        intent_overrides = {"contract_digest": "0" * 64}
    elif drift == "raw_request":
        intent_overrides = {"raw_request": "tampered task text"}
    seeded = _seed_pre_review_execution(
        tmp_path, monkeypatch, intent_overrides=intent_overrides,
    )
    candidate_fingerprint = "a" * 64
    reviewer_fingerprint = None
    request = dict(seeded.request)
    if drift == "source":
        (Path(seeded.snapshot.repo.workspace) / "base.txt").write_text(
            "drifted\n", encoding="utf-8",
        )
    elif drift == "workspace":
        other = (tmp_path / "other-workspace").resolve()
        other.mkdir()
        request["workspace"] = str(other)
    elif drift == "candidate_route":
        candidate_fingerprint = "b" * 64
    elif drift == "reviewer_route":
        reviewer_fingerprint = "c" * 64
    observed = _install_pre_review_recovery_runtime(
        seeded,
        monkeypatch,
        candidate_fingerprint=candidate_fingerprint,
        reviewer_fingerprint=reviewer_fingerprint,
    )
    before = seeded.review_store.get_execution_pipeline(seeded.plan_id)
    old_inode = seeded.old_sentinel.stat().st_ino

    with pytest.raises(
        delegate_tool.BestplanReviewRecoveryDeferred,
        match=expected_code,
    ) as exc_info:
        delegate_tool.resume_bestplan_execution_request(request)

    assert exc_info.value.code == expected_code
    after = seeded.review_store.get_execution_pipeline(seeded.plan_id)
    assert after.next_attempt_ordinal == before.next_attempt_ordinal == 1
    assert after.active_attempt_ordinal == before.active_attempt_ordinal == 0
    assert after.attempt_owner_pid == before.attempt_owner_pid
    assert observed["runtime_plan_ids"] == []
    assert observed["preflights"] == []
    assert observed["batches"] == []
    assert seeded.old_sentinel.read_bytes() == b"do-not-touch"
    assert seeded.old_sentinel.stat().st_ino == old_inode
    assert sorted(path.name for path in seeded.old_root.parent.iterdir()) == [
        seeded.old_root.name,
    ]


@pytest.mark.parametrize("tracker_phase", ("running", "intent", "scheduled"))
@pytest.mark.parametrize("store_first", [True, False])
def test_startup_order_preserves_exact_pending_pre_review_execution(
    tmp_path,
    monkeypatch,
    store_first,
    tracker_phase,
):
    from agent.bestplan_state import BestplanStore, PlanState
    from tools import async_delegation

    seeded = _seed_pre_review_execution(tmp_path, monkeypatch)
    seeded.plan_store.close()
    tracker_payload = json.loads(seeded.tracker.read_text(encoding="utf-8"))
    entry = tracker_payload["records"][seeded.request["delegation_id"]]
    entry["status"] = tracker_phase
    entry["delivery_status"] = tracker_phase
    entry["record"]["status"] = tracker_phase
    entry["record"]["delivery_status"] = tracker_phase
    seeded.tracker.write_text(
        json.dumps(tracker_payload), encoding="utf-8",
    )
    recovery_queue: queue.Queue = queue.Queue()

    if store_first:
        startup_store = BestplanStore(db_path=seeded.state_db)
        startup_store.close()
    recovered = async_delegation.recover_async_delegations(
        seeded.tracker,
        target_queue=queue.Queue(),
        review_recovery_queue=recovery_queue,
    )
    if not store_first:
        startup_store = BestplanStore(db_path=seeded.state_db)
        startup_store.close()

    assert recovered == {"queued": 0, "lost": 0, "review_requeued": 1}
    request = recovery_queue.get_nowait()
    assert request == seeded.request
    row = BestplanStore(db_path=seeded.state_db).get_plan(seeded.plan_id)
    assert row["state"] in {PlanState.RUNNING, PlanState.WAITING}
    assert row["dispatch_state"] != "terminal"
    assert row["error"] != "recapture_required"
    pipeline = seeded.review_store.get_execution_pipeline(seeded.plan_id)
    assert pipeline.state == "pending"
    assert pipeline.next_attempt_ordinal == 1


def test_startup_reconcile_uses_second_dead_pipeline_owner_for_next_takeover(
    tmp_path,
    monkeypatch,
):
    """A stale tracker owner must not invalidate a newer pipeline claim."""

    from agent.bestplan_state import BestplanStore, PlanState

    seeded = _seed_pre_review_execution(tmp_path, monkeypatch)
    seeded.plan_store.close()
    second_owner_pid = 888_888_888
    second_owner_start = "second-dead-owner-start"
    assert seeded.review_store.allocate_execution_attempt(
        seeded.plan_id,
        owner_pid=second_owner_pid,
        owner_process_start_id=second_owner_start,
        expected_owner_pid=999_999_999,
        expected_owner_process_start_id="dead-owner-start",
    ) == 1

    tracker_payload = json.loads(seeded.tracker.read_text(encoding="utf-8"))
    entry = tracker_payload["records"][seeded.request["delegation_id"]]
    entry["status"] = "running"
    entry["delivery_status"] = "running"
    entry["record"]["status"] = "running"
    entry["record"]["delivery_status"] = "running"
    seeded.tracker.write_text(json.dumps(tracker_payload), encoding="utf-8")

    observed_owners = []

    def owner_liveness(record):
        observed_owners.append(
            (record.get("owner_pid"), record.get("owner_started_at"))
        )
        return False

    monkeypatch.setattr(
        "tools.async_delegation._owner_liveness", owner_liveness,
    )
    startup_store = BestplanStore(
        db_path=seeded.state_db, reconcile_push_state=False,
    )
    row = startup_store.get_plan(seeded.plan_id)
    startup_store.close()

    assert observed_owners == [(second_owner_pid, second_owner_start)]
    assert row["state"] in {PlanState.RUNNING, PlanState.WAITING}
    assert row["dispatch_state"] != "terminal"
    assert row["error"] != "recapture_required"
    assert seeded.review_store.allocate_execution_attempt(
        seeded.plan_id,
        owner_pid=777_777_777,
        owner_process_start_id="third-owner-start",
        expected_owner_pid=second_owner_pid,
        expected_owner_process_start_id=second_owner_start,
    ) == 2


@pytest.mark.parametrize(
    ("corruption", "intent_overrides", "candidate_overrides", "runtime_routes"),
    [
        ("contract", {"contract_digest": "0" * 64}, None, None),
        ("source", {"source_snapshot_digest": "0" * 64}, None, None),
        (
            "candidate_fingerprint",
            None,
            {"runtime_fingerprint": "b" * 64},
            None,
        ),
        (
            "non_mapping_route",
            None,
            None,
            ["malformed", *_review_routes()],
        ),
    ],
)
def test_startup_reconciliation_terminalizes_stale_execution_intent(
    tmp_path,
    monkeypatch,
    corruption,
    intent_overrides,
    candidate_overrides,
    runtime_routes,
):
    del corruption
    from agent.bestplan_state import BestplanStore

    seeded = _seed_pre_review_execution(
        tmp_path,
        monkeypatch,
        intent_overrides=intent_overrides,
        candidate_route_overrides=candidate_overrides,
        corrupt_runtime_routes=runtime_routes,
    )
    seeded.plan_store.close()
    tracker_payload = json.loads(seeded.tracker.read_text(encoding="utf-8"))
    entry = tracker_payload["records"][seeded.request["delegation_id"]]
    entry["status"] = "running"
    entry["delivery_status"] = "running"
    entry["record"]["status"] = "running"
    entry["record"]["delivery_status"] = "running"
    seeded.tracker.write_text(json.dumps(tracker_payload), encoding="utf-8")
    monkeypatch.setattr(
        "tools.async_delegation._owner_liveness", lambda _record: True,
    )

    startup_store = BestplanStore(db_path=seeded.state_db)
    row = startup_store.get_plan(seeded.plan_id)
    startup_store.close()

    assert row["dispatch_state"] == "terminal"
    assert row["error"] == "recapture_required"


@pytest.mark.parametrize(
    "owner_update",
    (
        "attempt_owner_process_start_id=NULL",
        "next_attempt_ordinal=next_attempt_ordinal+1",
    ),
)
def test_startup_reconciliation_fails_closed_on_malformed_pipeline_owner(
    tmp_path,
    monkeypatch,
    owner_update,
):
    from agent.bestplan_state import BestplanStore

    seeded = _seed_pre_review_execution(tmp_path, monkeypatch)
    seeded.plan_store.close()
    with sqlite3.connect(seeded.state_db) as connection:
        connection.execute(
            f"UPDATE bestplan_execution_pipelines SET {owner_update} WHERE plan_id=?",
            (seeded.plan_id,),
        )
    tracker_payload = json.loads(seeded.tracker.read_text(encoding="utf-8"))
    entry = tracker_payload["records"][seeded.request["delegation_id"]]
    entry["status"] = "running"
    entry["delivery_status"] = "running"
    entry["record"]["status"] = "running"
    entry["record"]["delivery_status"] = "running"
    seeded.tracker.write_text(json.dumps(tracker_payload), encoding="utf-8")
    monkeypatch.setattr(
        "tools.async_delegation._owner_liveness", lambda _record: True,
    )

    startup_store = BestplanStore(db_path=seeded.state_db)
    row = startup_store.get_plan(seeded.plan_id)
    startup_store.close()

    assert row["dispatch_state"] == "terminal"
    assert row["error"] == "recapture_required"


@pytest.mark.parametrize("tampered_hash", [False, True])
def test_post_review_job_restart_preserves_exact_raw_task_and_hash(
    tmp_path,
    monkeypatch,
    tampered_hash,
):
    from agent import review_engine
    from tools import delegate_tool

    seeded = _seed_pre_review_execution(tmp_path, monkeypatch)
    raw_request = "Implement the exact task after the ReviewJob restart"
    request_digest = hashlib.sha256(raw_request.encode("utf-8")).hexdigest()
    if tampered_hash:
        request_digest = "0" * 64
    pipeline = seeded.review_store.get_execution_pipeline(seeded.plan_id)
    job = seeded.review_store.create_job(
        job_id=pipeline.job_id,
        source_kind="bestplan_integration",
        source_id=seeded.plan_id,
        target_digest="1" * 64,
        policy_digest="2" * 64,
        integration_oid="3" * 40,
        check_receipt_digest="4" * 64,
        adapter_version="local-bestplan.v1",
        owner_session_id="local-session",
        owner_profile="coder",
        workspace=seeded.snapshot.repo.workspace,
        adapter_state={
            "schema": "hermes.bestplan.local-review-adapter.v1",
            "raw_request": raw_request,
            "raw_request_sha256": request_digest,
        },
        runtime_routes=_review_routes(),
    )
    adapter = delegate_tool.LocalBestplanReviewRecoveryAdapter()
    adapter.bind_request({"state_db_path": str(seeded.state_db)})

    if tampered_hash:
        with pytest.raises(
            delegate_tool.BestplanReviewRecoveryDeferred,
            match="review_checkpoint_invalid",
        ):
            adapter._load_plan_context(job=job)
    else:
        context = adapter._load_plan_context(job=job)
        assert context["raw_request"] == raw_request
        assert context["plan_row"]["raw_request"] == ""


def test_existing_exact_review_job_wins_execution_marker_crash_window(
    tmp_path,
    monkeypatch,
):
    from tools import async_delegation, delegate_tool

    seeded = _seed_pre_review_execution(tmp_path, monkeypatch)
    raw_request = "Continue the exact review after job creation"
    pipeline = seeded.review_store.get_execution_pipeline(seeded.plan_id)
    seeded.review_store.create_job(
        job_id=pipeline.job_id,
        source_kind="bestplan_integration",
        source_id=seeded.plan_id,
        target_digest="1" * 64,
        policy_digest="2" * 64,
        integration_oid="3" * 40,
        check_receipt_digest="4" * 64,
        adapter_version="local-bestplan.v1",
        owner_session_id="local-session",
        owner_profile="coder",
        workspace=seeded.snapshot.repo.workspace,
        adapter_state={
            "schema": "hermes.bestplan.local-review-adapter.v1",
            "raw_request": raw_request,
            "raw_request_sha256": hashlib.sha256(
                raw_request.encode("utf-8")
            ).hexdigest(),
        },
        runtime_routes=_review_routes(),
    )
    tracker_payload = json.loads(seeded.tracker.read_text(encoding="utf-8"))
    entry = tracker_payload["records"][seeded.request["delegation_id"]]
    entry["status"] = "running"
    entry["delivery_status"] = "running"
    entry["record"]["status"] = "running"
    entry["record"]["delivery_status"] = "running"
    seeded.tracker.write_text(json.dumps(tracker_payload), encoding="utf-8")
    recovery_queue: queue.Queue = queue.Queue()

    recovered = async_delegation.recover_async_delegations(
        seeded.tracker,
        target_queue=queue.Queue(),
        review_recovery_queue=recovery_queue,
    )
    recovered_request = recovery_queue.get_nowait()
    assert recovered == {"queued": 0, "lost": 0, "review_requeued": 1}
    assert recovered_request["kind"] == "bestplan_review_resume"
    assert recovered_request["adapter_version"] == "local-bestplan.v1"

    review_calls = []
    monkeypatch.setattr(
        async_delegation,
        "_profile_home_for_bestplan_recovery",
        lambda _request: tmp_path,
    )
    monkeypatch.setattr(
        delegate_tool,
        "resume_bestplan_execution_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("candidate execution replayed after ReviewJob")
        ),
    )
    monkeypatch.setattr(
        delegate_tool,
        "resume_bestplan_review_request",
        lambda request, **_kwargs: review_calls.append(dict(request)) or {
            "status": "checkpoint_advanced",
        },
    )

    # A request queued just before create_job must also converge to review.
    result = async_delegation._default_bestplan_review_recovery_worker(
        dict(seeded.request)
    )

    assert result == {"status": "checkpoint_advanced"}
    assert review_calls == [recovered_request]
    assert seeded.review_store.get_execution_pipeline(seeded.plan_id).state == (
        "pending"
    )


def test_initial_check_timeout_is_durable_waiting_and_retried_not_terminal(
    tmp_path, monkeypatch,
):
    """A transient first-check failure must enter the recovery rail."""

    from agent import bestplan_checks, review_engine
    from tests.agent.test_bestplan_local_flow import _review_loop_inputs
    from tools import async_delegation, delegate_tool
    from tools.process_registry import process_registry

    inputs = _review_loop_inputs(tmp_path)
    tracker = (tmp_path / "async-delegations.json").resolve()
    state_db = (tmp_path / "state.db").resolve()
    delegation_id = "delegation-initial-check-timeout"
    job_id = delegate_tool._bestplan_safe_identifier(
        "review-job", "bp-local"
    )
    retry_requests: list[tuple[dict[str, object], str]] = []
    completion_queue: queue.Queue = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", completion_queue)
    monkeypatch.setattr(
        async_delegation,
        "_start_bestplan_review_recovery_consumer",
        lambda: None,
    )
    monkeypatch.setattr(
        async_delegation,
        "_schedule_bestplan_review_recovery_retry",
        lambda request, *, reason_code: retry_requests.append(
            (dict(request), reason_code)
        ),
    )
    async_delegation._reset_for_tests()

    reviewers = (
        SimpleNamespace(
            slot="smart_reviewer",
            provider="anthropic",
            model="claude-review",
            model_family="claude",
            runtime_fingerprint="d" * 64,
            authority=object(),
        ),
        SimpleNamespace(
            slot="code_worker",
            provider="openai-codex",
            model="gpt-review",
            model_family="gpt",
            runtime_fingerprint="e" * 64,
            authority=object(),
        ),
    )
    candidate_routes = (
        {
            "route": "code_worker",
            "provider": "provider-a",
            "model": "candidate-a",
            "runtime_fingerprint": "a" * 64,
        },
        {
            "route": "deep_research",
            "provider": "provider-b",
            "model": "candidate-b",
            "runtime_fingerprint": "b" * 64,
        },
    )

    def runner():
        return delegate_tool._recover_initial_bestplan_check_failure(
            plan_id="bp-local",
            raw_request="Repair the transient check failure",
            plan=inputs.plan,
            snapshot=inputs.snapshot,
            contract={"schema": "test.local-review", "commands": []},
            approval_digest="5" * 64,
            contract_digest="6" * 64,
            completed=inputs.completed,
            candidate_authorities=(object(), object()),
            review_authority_bindings=reviewers,
            candidate_runtime_routes=candidate_routes,
            integration=inputs.integration(0),
            check_error=bestplan_checks.CheckExecutionError(
                "check deadline expired"
            ),
            runtime=inputs.runtime,
            state_db_path=state_db,
            session_id="session-local",
            profile="default",
            deadline=time.monotonic() + 30,
            cancel_event=None,
            projected_results=(
                {"status": "frozen", "summary": "initial"},
            ),
        )

    try:
        dispatch = async_delegation.dispatch_async_delegation_batch(
            goals=["Repair the transient check failure"],
            context="Isolated BestPlan candidate execution",
            toolsets=None,
            role="leaf",
            model="candidate-a",
            session_key="session-key",
            runner=runner,
            max_async_children=1,
            delegation_id=delegation_id,
            origin_session_id="session-local",
            origin_profile="default",
            origin_tracker_path=str(tracker),
            bestplan_plan_id="bp-local",
            bestplan_state_db_path=str(state_db),
            bestplan_review_job_id=job_id,
            bestplan_local_execution=True,
            resolved_runtimes=list(candidate_routes),
        )

        assert dispatch["status"] == "dispatched"
        deadline = time.monotonic() + 5
        entry = None
        while time.monotonic() < deadline:
            if tracker.exists():
                persisted = json.loads(tracker.read_text(encoding="utf-8"))
                entry = (persisted.get("records") or {}).get(delegation_id)
                status = str(
                    (entry or {}).get("status")
                    or ((entry or {}).get("record") or {}).get("status")
                    or ""
                )
                if status in {"review_waiting", "review_requeued", "error"}:
                    break
            time.sleep(0.02)

        assert entry is not None
        status = str(
            entry.get("status")
            or (entry.get("record") or {}).get("status")
            or ""
        )
        assert status in {"review_waiting", "review_requeued"}
        assert entry.get("event") is None
        assert entry.get("result") is None
        durable = review_engine.ReviewStore(state_db).get_job(job_id)
        assert durable.state == "queued"
        assert durable.cancel_requested is False
        pending = review_engine.ReviewStore(state_db).resume_job(
            job_id=job_id,
            owner_id=durable.owner_id,
            fencing_token=durable.fencing_token,
        )
        assert pending.next_action == "initial_checks"
        assert not any(
            event.kind == "host_check_failed"
            for event in review_engine.ReviewStore(state_db).list_events(job_id)
        )
        assert completion_queue.empty()
        assert status == "review_requeued" or retry_requests
        if retry_requests:
            assert retry_requests[-1][1] == "checks_runtime_unavailable"

        adapter = delegate_tool.LocalBestplanReviewRecoveryAdapter()
        adapter._state_db_path = state_db
        adapter._bound_plan_context = {
            "adapter_state": json.loads(durable.adapter_state_json),
            "plan_row": {},
            "raw_request": "Repair the transient check failure",
            "validated": SimpleNamespace(
                approval_digest="5" * 64,
                contract={"schema": "test.local-review", "commands": []},
                plan=inputs.plan,
                source_snapshot=inputs.snapshot,
            ),
        }
        adapter._execution_context = lambda **_kwargs: {
            "runtime": inputs.runtime,
        }
        monkeypatch.setattr(
            bestplan_checks,
            "run_integration_checks",
            lambda **_kwargs: (_ for _ in ()).throw(
                bestplan_checks.CheckExecutionError(
                    "enrollment-bound check returned nonzero"
                )
            ),
        )
        transition = adapter.initial_checks(
            request={},
            store=review_engine.ReviewStore(state_db),
            job=durable,
            claim=SimpleNamespace(
                owner_id=durable.owner_id,
                fencing_token=durable.fencing_token,
            ),
            resume=pending,
        )
        blocked = review_engine.ReviewStore(state_db).get_job(job_id)
        next_resume = review_engine.ReviewStore(state_db).resume_job(
            job_id=job_id,
            owner_id=blocked.owner_id,
            fencing_token=blocked.fencing_token,
        )
        assert transition == {"status": "checkpoint_advanced"}
        assert next_resume.next_action == "repair"
        assert json.loads(blocked.adapter_state_json).get(
            "initial_check_pending"
        )
    finally:
        async_delegation._reset_for_tests()


def test_startup_restore_uses_owned_review_queue_and_live_worker(
    tmp_path, monkeypatch,
):
    from tools import async_delegation

    handled = []
    completed = threading.Event()
    recovery_queue: queue.Queue = queue.Queue()
    monkeypatch.setattr(
        async_delegation,
        "_bestplan_review_recovery_queue",
        recovery_queue,
        raising=False,
    )
    monkeypatch.setattr(
        async_delegation,
        "_bestplan_review_recovery_thread",
        None,
        raising=False,
    )

    def recover(*, target_queue, mark_restored, review_recovery_queue):
        assert target_queue is completion_queue
        assert mark_restored is True
        assert review_recovery_queue is recovery_queue
        review_recovery_queue.put({"kind": "bestplan_review_resume"})
        return {"queued": 0, "lost": 0, "review_requeued": 1}

    def worker(request):
        handled.append(request)
        completed.set()
        return {"status": "resumed"}

    monkeypatch.setattr(async_delegation, "recover_async_delegations", recover)
    monkeypatch.setattr(
        async_delegation,
        "_default_bestplan_review_recovery_worker",
        worker,
        raising=False,
    )
    monkeypatch.setattr(
        async_delegation,
        "_db_path",
        lambda: str(tmp_path / "absent.db"),
    )
    completion_queue: queue.Queue = queue.Queue()

    assert async_delegation.restore_undelivered_completions(completion_queue) == 0
    assert completed.wait(timeout=2)
    assert handled == [{"kind": "bestplan_review_resume"}]


def test_omitted_review_queue_routes_recovery_to_owned_consumer(
    tmp_path, monkeypatch,
):
    from tools import async_delegation

    owned: queue.Queue = queue.Queue()
    started = []
    monkeypatch.setattr(
        async_delegation, "_bestplan_review_recovery_queue", owned,
    )
    monkeypatch.setattr(
        async_delegation,
        "_start_bestplan_review_recovery_consumer",
        lambda: started.append(True),
    )
    tracker = tmp_path / "empty.json"
    tracker.write_text('{"version":1,"records":{}}', encoding="utf-8")

    async_delegation.recover_async_delegations(
        tracker,
        target_queue=queue.Queue(),
    )

    assert started == [True]


@pytest.mark.parametrize(
    "reason_code",
    (
        "review_runtime_unavailable",
        "review_job_unavailable",
        "repair_runtime_unavailable",
        "checks_runtime_unavailable",
        "landing_runtime_unavailable",
        "landing_claim_active",
        "landing_effect_unknown",
        "completion_persist_failed",
    ),
)
def test_transient_recovery_failure_requeues_with_bounded_backoff(
    monkeypatch, reason_code,
):
    from tools import async_delegation
    from tools.delegate_tool import BestplanReviewRecoveryDeferred

    pending: queue.Queue = queue.Queue()
    pending.put({"kind": "bestplan_review_resume", "delegation_id": "d1"})
    timers = []
    monkeypatch.setattr(
        async_delegation,
        "_defer_bestplan_review_recovery",
        lambda request, **kwargs: timers.append(("defer", request, kwargs)),
    )
    monkeypatch.setattr(
        async_delegation,
        "_schedule_bestplan_review_recovery_retry",
        lambda request, **kwargs: timers.append(("retry", request, kwargs)),
        raising=False,
    )

    consumed = async_delegation.consume_bestplan_review_recoveries(
        pending,
        worker=lambda _request: (_ for _ in ()).throw(
            BestplanReviewRecoveryDeferred(reason_code)
        ),
        max_items=1,
    )

    assert consumed == {"consumed": 1, "completed": 0, "deferred": 1}
    assert timers[0][0] == "defer"
    assert timers[1] == (
        "retry",
        {"kind": "bestplan_review_resume", "delegation_id": "d1"},
        {"reason_code": reason_code},
    )


def test_unknown_operational_recovery_error_retries_then_completes(monkeypatch):
    import sqlite3

    from tools import async_delegation

    request = {"kind": "bestplan_review_resume", "delegation_id": "d1"}
    pending: queue.Queue = queue.Queue()
    pending.put(dict(request))
    scheduled = []
    attempts = []
    monkeypatch.setattr(
        async_delegation,
        "_defer_bestplan_review_recovery",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        async_delegation,
        "_schedule_bestplan_review_recovery_retry",
        lambda item, **kwargs: scheduled.append((dict(item), kwargs)),
    )

    def worker(_request):
        attempts.append("attempt")
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is temporarily busy")
        return {
            "status": "completed",
            "completion": {"results": [{"status": "frozen"}]},
        }

    monkeypatch.setattr(
        async_delegation,
        "_complete_bestplan_review_recovery",
        lambda *_args: True,
    )
    first = async_delegation.consume_bestplan_review_recoveries(
        pending, worker=worker, max_items=1,
    )
    assert first == {"consumed": 1, "completed": 0, "deferred": 1}
    assert scheduled == [
        (request, {"reason_code": "OperationalError"}),
    ]

    pending.put(scheduled.pop()[0])
    second = async_delegation.consume_bestplan_review_recoveries(
        pending, worker=worker, max_items=1,
    )
    assert second == {"consumed": 1, "completed": 1, "deferred": 0}


def test_defer_write_failure_requeues_and_next_attempt_completes(monkeypatch):
    import sqlite3

    from tools import async_delegation

    request = {"kind": "bestplan_review_resume", "delegation_id": "d1"}
    pending: queue.Queue = queue.Queue()
    pending.put(dict(request))
    attempts: list[str] = []
    defer_attempts: list[str] = []

    def defer(_request, **_kwargs):
        defer_attempts.append("defer")
        raise OSError("tracker replacement failed")

    def schedule(item, **_kwargs):
        pending.put(dict(item))

    monkeypatch.setattr(
        async_delegation, "_defer_bestplan_review_recovery", defer,
    )
    monkeypatch.setattr(
        async_delegation, "_schedule_bestplan_review_recovery_retry", schedule,
    )
    monkeypatch.setattr(
        async_delegation,
        "_complete_bestplan_review_recovery",
        lambda *_args: True,
    )

    def worker(_request):
        attempts.append("attempt")
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is temporarily busy")
        return {
            "status": "completed",
            "completion": {"results": [{"status": "frozen"}]},
        }

    first = async_delegation.consume_bestplan_review_recoveries(
        pending, worker=worker, max_items=1,
    )
    second = async_delegation.consume_bestplan_review_recoveries(
        pending, worker=worker, max_items=1,
    )

    assert first == {"consumed": 1, "completed": 0, "deferred": 1}
    assert second == {"consumed": 1, "completed": 1, "deferred": 0}
    assert defer_attempts == ["defer"]
    assert attempts == ["attempt", "attempt"]
    assert pending.empty()


@pytest.mark.parametrize(
    ("consumer_name", "drain_name", "wake_name", "queue_name"),
    (
        (
            "_bestplan_review_recovery_consumer",
            "consume_bestplan_review_recoveries",
            "_bestplan_review_recovery_wake",
            "_bestplan_review_recovery_queue",
        ),
        (
            "_manual_review_recovery_consumer",
            "consume_manual_review_recoveries",
            "_manual_review_recovery_wake",
            "_manual_review_recovery_queue",
        ),
    ),
)
def test_recovery_daemon_survives_one_failed_drain(
    monkeypatch, consumer_name, drain_name, wake_name, queue_name,
):
    from tools import async_delegation

    class ImmediateWake:
        def wait(self):
            return True

        def clear(self):
            return None

    calls: list[str] = []

    def drain(*_args, **_kwargs):
        calls.append("drain")
        if len(calls) == 1:
            raise RuntimeError("one failed recovery drain")
        raise KeyboardInterrupt

    monkeypatch.setattr(async_delegation, wake_name, ImmediateWake())
    monkeypatch.setattr(async_delegation, queue_name, queue.Queue())
    monkeypatch.setattr(async_delegation, drain_name, drain)

    with pytest.raises(KeyboardInterrupt):
        getattr(async_delegation, consumer_name)()

    assert calls == ["drain", "drain"]


def test_checkpoint_invalid_is_terminal_error_not_operator_wait(
    tmp_path, monkeypatch,
):
    from tools import async_delegation
    from tools.delegate_tool import BestplanReviewRecoveryDeferred
    from tools.process_registry import process_registry

    tracker = (tmp_path / "tracker.json").resolve()
    state_db = (tmp_path / "state.db").resolve()
    record = {
        "delegation_id": "d-integrity",
        "status": "review_requeued",
        "delivery_status": "review_requeued",
        "bestplan_plan_id": "plan-1",
        "bestplan_review_job_id": "job-1",
        "bestplan_local_execution": True,
        "bestplan_state_db_path": str(state_db),
        "origin_tracker_path": str(tracker),
        "dispatched_at": time.time(),
        "is_batch": True,
    }
    tracker.write_text(json.dumps({
        "version": 1,
        "records": {"d-integrity": {
            "delegation_id": "d-integrity",
            "record": record,
            "status": "review_requeued",
            "delivery_status": "review_requeued",
        }},
    }), encoding="utf-8")
    async_delegation._records["d-integrity"] = dict(record)
    completions: queue.Queue = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", completions)
    monkeypatch.setattr(
        async_delegation,
        "_mark_bestplan_completed_unverified",
        lambda *_args: True,
    )
    scheduled = []
    monkeypatch.setattr(
        async_delegation,
        "_schedule_bestplan_review_recovery_retry",
        lambda *_args, **_kwargs: scheduled.append(True),
    )
    pending: queue.Queue = queue.Queue()
    pending.put({
        "kind": "bestplan_review_resume",
        "delegation_id": "d-integrity",
        "job_id": "job-1",
        "tracker_path": str(tracker),
    })

    consumed = async_delegation.consume_bestplan_review_recoveries(
        pending,
        worker=lambda _request: (_ for _ in ()).throw(
            BestplanReviewRecoveryDeferred("review_checkpoint_invalid")
        ),
        max_items=1,
    )

    assert consumed == {"consumed": 1, "completed": 0, "deferred": 0}
    persisted = json.loads(tracker.read_text(encoding="utf-8"))["records"][
        "d-integrity"
    ]
    assert persisted["status"] == "error"
    assert persisted["record"]["status"] == "error"
    assert completions.get_nowait()["status"] == "error"
    assert scheduled == []


def test_recovery_cancel_finalizes_only_after_worker_unwinds(tmp_path):
    from agent.review_engine import ReviewStore, ReviewTarget
    from tools import async_delegation
    from tools.delegate_tool import BestplanReviewRecoveryDeferred

    state_db = (tmp_path / "state.db").resolve()
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    store = ReviewStore(state_db)
    target = ReviewTarget.bestplan_integration(
        plan_id="bp-local",
        generation=0,
        base_oid="1" * 40,
        local_target_oid="1" * 40,
        integration_oid="2" * 40,
        integration_tree_oid="3" * 40,
        integration_ref="refs/hermes-bestplan-integrations/bp-local/0",
        integration_receipt_digest="4" * 64,
        check_receipt_digest="5" * 64,
        approval_digest="6" * 64,
        contract_digest="7" * 64,
        diff_sha256="8" * 64,
        acceptance_digest="9" * 64,
        policy_digest="a" * 64,
    )
    job = store.create_job(
        job_id="review-job-local",
        source_kind=target.source_kind,
        source_id="bp-local",
        target_digest=target.target_digest,
        policy_digest=target.policy_digest,
        integration_oid=target.integration_oid,
        check_receipt_digest=target.check_receipt_digest,
        adapter_version="local-bestplan.v1",
        owner_session_id="session-1",
        owner_profile="coder",
        workspace=str(workspace),
    )
    claim = store.claim_job(
        job_id=job.job_id,
        owner_id="review-worker",
        now_ns=1,
        lease_duration_ns=10**18,
        expected_fencing_token=job.fencing_token,
    )
    delegation_id = "delegation-cancel-finalize"
    record = {
        "delegation_id": delegation_id,
        "bestplan_plan_id": "bp-local",
        "bestplan_local_execution": True,
        "bestplan_state_db_path": str(state_db),
        "bestplan_review_job_id": job.job_id,
        "status": "review_requeued",
        "delivery_status": "review_requeued",
        "is_batch": True,
        "dispatched_at": time.time(),
    }
    async_delegation._records[delegation_id] = record
    pending: queue.Queue = queue.Queue()
    pending.put({
        "kind": "bestplan_review_resume",
        "delegation_id": delegation_id,
        "job_id": job.job_id,
    })
    observed = []

    def worker(_request, *, cancel_event):
        store.request_cancel(
            job_id=job.job_id,
            owner_id=claim.owner_id,
            fencing_token=claim.fencing_token,
            operation_id="cancel-during-worker",
            signal_children=cancel_event.set,
        )
        observed.append(store.get_job(job.job_id).state)
        raise BestplanReviewRecoveryDeferred("review_cancelled")

    async_delegation.consume_bestplan_review_recoveries(
        pending, worker=worker, max_items=1,
    )

    assert observed == ["cancel_requested"]
    assert store.get_job(job.job_id).state == "cancelled"
    assert async_delegation._records[delegation_id]["status"] == "interrupted"


def test_identity_drift_stays_waiting_without_automatic_retry(monkeypatch):
    from tools import async_delegation
    from tools.delegate_tool import BestplanReviewRecoveryDeferred

    pending: queue.Queue = queue.Queue()
    pending.put({"kind": "bestplan_review_resume", "delegation_id": "d1"})
    scheduled = []
    monkeypatch.setattr(
        async_delegation,
        "_defer_bestplan_review_recovery",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        async_delegation,
        "_schedule_bestplan_review_recovery_retry",
        lambda *args, **kwargs: scheduled.append((args, kwargs)),
        raising=False,
    )

    async_delegation.consume_bestplan_review_recoveries(
        pending,
        worker=lambda _request: (_ for _ in ()).throw(
            BestplanReviewRecoveryDeferred(
                "review_runtime_fingerprint_changed"
            )
        ),
        max_items=1,
    )

    assert scheduled == []


def test_landed_completion_persist_failure_is_retried(monkeypatch):
    from tools import async_delegation

    pending: queue.Queue = queue.Queue()
    request = {
        "kind": "bestplan_review_resume",
        "delegation_id": "d-landed",
        "job_id": "job-landed",
    }
    pending.put(request)
    scheduled = []
    monkeypatch.setattr(
        async_delegation,
        "_complete_bestplan_review_recovery",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        async_delegation,
        "_defer_bestplan_review_recovery",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        async_delegation,
        "_schedule_bestplan_review_recovery_retry",
        lambda request, *, reason_code: scheduled.append(
            (dict(request), reason_code)
        ),
    )

    consumed = async_delegation.consume_bestplan_review_recoveries(
        pending,
        worker=lambda _request: {
            "status": "resumed",
            "result": {
                "status": "completed",
                "completion": {"results": []},
            },
        },
        max_items=1,
    )

    assert consumed == {"consumed": 1, "completed": 0, "deferred": 1}
    assert scheduled == [(request, "completion_persist_failed")]


def test_claimed_landing_recovery_and_landed_retry_never_replay_git(tmp_path):
    import gc

    from tests.agent.test_bestplan_landing_gate import (
        _git,
        _lock_probe,
        _prepare_landing,
    )
    from tools import delegate_tool

    prepared = _prepare_landing(tmp_path, recovery_identity=True)
    authorization = prepared.store.claim_landing(
        "bp-local",
        owner_id="review-worker",
        fencing_token=prepared.review_claim.fencing_token,
        owner_pid=999_999_999,
        owner_process_start_id="kernel-start:1",
        operation_id="claimed-before-crash",
    )
    lock_path = authorization.repository_effect_lock_path
    _git(
        prepared.repo,
        "merge",
        "--ff-only",
        prepared.integration.integration_oid,
    )
    del authorization
    gc.collect()
    assert _lock_probe(lock_path).returncode == 0
    request = {
        "adapter_version": "local-bestplan.v1",
        "delegation_id": "delegation-landed-crash",
        "job_id": "review-job-local",
        "kind": "bestplan_review_resume",
        "plan_id": "bp-local",
        "profile": "coder",
        "session_id": "session-1",
        "state_db_path": str(prepared.store.state_db_path.resolve()),
        "tracker_path": str((tmp_path / "tracker.json").resolve()),
        "workspace": str(prepared.snapshot.repo.workspace),
    }

    recovered = delegate_tool.resume_bestplan_review_request(
        request, adapter=None,
    )
    repeated = delegate_tool.resume_bestplan_review_request(
        request, adapter=None,
    )

    assert recovered["next_action"] == "reconcile_landing"
    assert recovered["result"]["status"] == "completed"
    assert repeated["next_action"] == "complete_landed"
    assert repeated["result"] == recovered["result"]
    assert prepared.review_store.get_job("review-job-local").state == "landed"
    assert _git(prepared.repo, "rev-parse", "HEAD") == (
        prepared.integration.integration_oid
    )


def test_recovery_reviewer_failures_retry_but_authority_findings_wait(
    monkeypatch,
):
    from agent import review_engine
    from tools import delegate_tool

    adapter = delegate_tool.LocalBestplanReviewRecoveryAdapter()
    adapter._review_bindings = (
        SimpleNamespace(
            slot="smart_reviewer",
            provider="anthropic",
            model="review-a",
            model_family="claude",
            runtime_fingerprint="1" * 64,
        ),
        SimpleNamespace(
            slot="code_worker",
            provider="openai-codex",
            model="review-b",
            model_family="gpt",
            runtime_fingerprint="2" * 64,
        ),
    )
    adapter._load_context = lambda **_kwargs: {
        "bundle": SimpleNamespace(
            artifact=object(), evidence=object(), packet=object(),
            target=SimpleNamespace(
                target_digest="3" * 64,
                integration_oid="4" * 40,
                check_receipt_digest="5" * 64,
            ),
        )
    }
    waits = []
    store = SimpleNamespace(
        wait_for_host=lambda **kwargs: waits.append(kwargs),
    )
    inputs = {
        "store": store,
        "job": SimpleNamespace(job_id="job-1", source_id="plan-1"),
        "claim": SimpleNamespace(owner_id="owner-1", fencing_token=1),
        "resume": SimpleNamespace(
            generation=0,
            adopted_reviewer_receipts=(),
            missing_reviewer_slots=("smart_reviewer", "code_worker"),
            target_digest="3" * 64,
        ),
    }

    monkeypatch.setattr(
        review_engine,
        "run_review_generation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("provider unavailable")
        ),
    )
    with pytest.raises(
        delegate_tool.BestplanReviewRecoveryDeferred
    ) as transient:
        adapter.review_missing_slots(**inputs)
    assert transient.value.code == "review_runtime_unavailable"

    monkeypatch.setattr(
        review_engine,
        "run_review_generation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            review_engine.ReviewRequiresAuthority(
                "review cites a path outside the approved lease"
            )
        ),
    )
    assert adapter.review_missing_slots(**inputs) == {
        "status": "blocked_requires_authority"
    }
    assert waits[-1]["reason_code"] == "blocked_requires_authority"


def test_recovery_reviewer_retry_gets_fresh_one_shot_authorities(monkeypatch):
    from agent import bestplan_local, review_engine
    from tools import delegate_tool

    class OneShotAuthority:
        next_id = 0

        def __init__(self):
            self.clone_ids = []
            self.clone_id = None
            self.used = False

        def clone_for_review(self):
            clone = OneShotAuthority()
            clone.clone_id = OneShotAuthority.next_id
            OneShotAuthority.next_id += 1
            self.clone_ids.append(clone.clone_id)
            return clone

    roots = (OneShotAuthority(), OneShotAuthority())
    adapter = delegate_tool.LocalBestplanReviewRecoveryAdapter()
    adapter._review_bindings = tuple(
        bestplan_local.LocalReviewAuthorityBinding(
            slot=slot,
            provider=provider,
            model=model,
            model_family=family,
            runtime_fingerprint=fingerprint,
            authority=authority,
        )
        for slot, provider, model, family, fingerprint, authority in (
            (
                "smart_reviewer", "anthropic", "review-a", "claude",
                "1" * 64, roots[0],
            ),
            (
                "code_worker", "openai-codex", "review-b", "gpt",
                "2" * 64, roots[1],
            ),
        )
    )
    target = SimpleNamespace(
        target_digest="3" * 64,
        integration_oid="4" * 40,
        check_receipt_digest="5" * 64,
    )
    adapter._load_context = lambda **_kwargs: {
        "bundle": SimpleNamespace(
            artifact=object(), evidence=object(), packet=object(),
            target=target,
        )
    }
    calls = []

    def call(binding, _request):
        authority = binding.authority
        assert authority.used is False
        authority.used = True
        calls.append(authority.clone_id)
        if len(calls) == 1:
            raise ConnectionError("provider unavailable once")
        return "fresh output"

    def generation(_target, bindings, **kwargs):
        for binding in bindings:
            kwargs["reviewer_call"](binding, {})
        return SimpleNamespace(passed=True, receipt_digest="6" * 64)

    monkeypatch.setattr(bestplan_local, "call_local_review_authority", call)
    monkeypatch.setattr(review_engine, "run_review_generation", generation)
    passes = []
    store = SimpleNamespace(
        record_generation_pass=lambda **kwargs: passes.append(kwargs),
    )
    inputs = {
        "store": store,
        "job": SimpleNamespace(job_id="job-1", source_id="plan-1"),
        "claim": SimpleNamespace(owner_id="owner-1", fencing_token=1),
        "resume": SimpleNamespace(
            generation=0,
            adopted_reviewer_receipts=(),
            missing_reviewer_slots=("smart_reviewer", "code_worker"),
            target_digest=target.target_digest,
        ),
    }

    with pytest.raises(delegate_tool.BestplanReviewRecoveryDeferred) as first:
        adapter.review_missing_slots(**inputs)
    assert first.value.code == "review_runtime_unavailable"
    assert adapter.review_missing_slots(**inputs) == {
        "status": "checkpoint_advanced"
    }
    assert len(passes) == 1
    assert calls == [0, 2, 3]
    assert roots[0].clone_ids == [0, 2]
    assert roots[1].clone_ids == [1, 3]


def test_expired_frozen_repair_is_adopted_without_provider_replay(
    tmp_path, monkeypatch,
):
    from dataclasses import replace

    from agent import bestplan_candidates, bestplan_promotion
    from tests.agent.test_bestplan_local_flow import _review_loop_inputs
    from tools import delegate_tool

    inputs = _review_loop_inputs(tmp_path)
    original_spec = inputs.completed[0][1]
    attempt_plan_id = delegate_tool._bestplan_safe_identifier(
        "repair-plan", "bp-local", 1, 0,
    )
    repair_spec = replace(
        original_spec,
        plan_id=attempt_plan_id,
        candidate_id=delegate_tool._bestplan_safe_identifier(
            "candidate", attempt_plan_id, 0, "slice-a",
        ),
        slice_id=delegate_tool._bestplan_safe_identifier(
            "slice", attempt_plan_id, 0, "slice-a",
        ),
        goal=original_spec.goal + "\n\nAutomatic review repair evidence:\n{}",
        expires_at=int(time.time()) + 60,
    )
    attempt_id = delegate_tool._bestplan_safe_identifier(
        "attempt", attempt_plan_id, 0, "slice-a", 0,
    )
    frozen = inputs.frozen(repair_spec, attempt_id, 1)
    receipt = delegate_tool._bestplan_host_candidate_receipt(
        frozen=frozen,
        manifest_slice_id="slice-a",
        spec=SimpleNamespace(expires_at=int(time.time()) - 3600),
        promotion_contract_digest="6" * 64,
    )
    receipt_json = json.dumps(
        receipt, sort_keys=True, separators=(",", ":"),
    )
    durable = SimpleNamespace(
        manifest_slice_id="slice-a",
        repair_attempt=0,
        candidate_receipt_json=receipt_json,
        changed_paths_json=json.dumps([
            path.decode("utf-8") for path in frozen.changed_paths
        ], sort_keys=True, separators=(",", ":")),
        prior_target_digest="d" * 64,
        base_integration_oid=inputs.integration(0).integration_oid,
        attempt_plan_id=attempt_plan_id,
    )
    adapter_state = {
        "schema": "hermes.bestplan.local-review-adapter.v1",
        "contract_digest": "6" * 64,
        "initial_check_failure": {
            "integration": delegate_tool._bestplan_review_integration_payload(
                inputs.integration(0)
            ),
        },
    }
    job = SimpleNamespace(
        adapter_state_json=json.dumps(adapter_state),
        job_id="review-job-local",
        policy_digest="e" * 64,
        source_id="bp-local",
    )
    blocker = {
        "blast_radius": "slice-a",
        "fingerprint": "f" * 64,
        "locator": {
            "end_line": 1,
            "kind": "changed_lines",
            "locator_id": None,
            "path": "slice-a/result.txt",
            "start_line": 1,
        },
        "observed_failure": "the approved behavior fails",
        "severity": "high",
        "title": "repair slice-a",
        "trigger": "the changed path is used",
    }
    resume = SimpleNamespace(
        adopted_reviewer_receipts=(),
        blocking_findings_json=json.dumps([blocker]),
        generation=0,
        target_digest="d" * 64,
    )
    recorded = []
    store = SimpleNamespace(
        list_events=lambda _job_id: (),
        list_repair_candidates=lambda *_args, **_kwargs: (durable,),
        record_repair_frozen=lambda **kwargs: recorded.append(kwargs),
    )
    adapter = delegate_tool.LocalBestplanReviewRecoveryAdapter()
    adapter._load_plan_context = lambda **_kwargs: {
        "adapter_state": adapter_state,
        "raw_request": "repair",
        "plan_row": {},
        "validated": SimpleNamespace(
            approval_digest="5" * 64,
            contract={"schema": "test"},
            plan=inputs.plan,
            source_snapshot=inputs.snapshot,
        ),
    }
    adapter._execution_context = lambda **_kwargs: {
        "candidate_authorities": (object(), object()),
        "prepared": tuple(
            {"spec": spec, "manifest_slice_id": slice_id}
            for _frozen, spec, slice_id in inputs.completed
        ),
        "runtime": inputs.runtime,
    }
    monkeypatch.setattr(
        bestplan_candidates,
        "run_and_freeze_repair_candidate",
        lambda **_kwargs: pytest.fail("provider replayed frozen repair"),
    )
    monkeypatch.setattr(
        bestplan_candidates, "_read_ref",
        lambda *_args, **_kwargs: frozen.commit_oid,
    )
    monkeypatch.setattr(
        bestplan_promotion, "freeze_repair_integration",
        lambda **_kwargs: inputs.integration(1),
    )

    assert adapter.repair(
        store=store,
        job=job,
        claim=SimpleNamespace(owner_id="owner", fencing_token=1),
        resume=resume,
    ) == {"status": "checkpoint_advanced"}
    assert len(recorded) == 1


def test_recovery_action_renews_its_lease_until_the_action_stops(
    tmp_path, monkeypatch,
):
    from agent import review_engine
    from tools import delegate_tool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_db = (tmp_path / "state.db").resolve()
    target = _target(review_engine)
    store = review_engine.ReviewStore(state_db)
    store.create_job(
        job_id="review-job-heartbeat",
        source_kind=target.source_kind,
        source_id=target.plan_id,
        target_digest=target.target_digest,
        policy_digest=target.policy_digest,
        integration_oid=target.integration_oid,
        check_receipt_digest=target.check_receipt_digest,
        adapter_version="local-bestplan.v1",
        owner_session_id="session-heartbeat",
        owner_profile="profile-heartbeat",
        workspace=str(workspace.resolve()),
        adapter_state={"schema": "heartbeat-test"},
        runtime_routes=[],
    )
    stale = store.claim_job(
        job_id="review-job-heartbeat",
        owner_id="dead-owner",
        now_ns=1,
        lease_duration_ns=1,
        expected_fencing_token=0,
    )
    store.begin_generation(
        job_id="review-job-heartbeat",
        generation=0,
        target=target,
        owner_id=stale.owner_id,
        fencing_token=stale.fencing_token,
        operation_id="heartbeat-generation",
    )
    started = threading.Event()
    release = threading.Event()

    class BlockingAdapter:
        def resolve_runtime_routes(self, **_kwargs):
            return []

        def review_missing_slots(self, **_kwargs):
            started.set()
            assert release.wait(2)
            return {"status": "checkpoint_advanced"}

    monkeypatch.setattr(
        delegate_tool, "_BESTPLAN_RECOVERY_LEASE_DURATION_NS", 60_000_000,
        raising=False,
    )
    monkeypatch.setattr(
        delegate_tool, "_BESTPLAN_RECOVERY_HEARTBEAT_SECONDS", 0.01,
        raising=False,
    )
    request = {
        "adapter_version": "local-bestplan.v1",
        "delegation_id": "delegation-heartbeat",
        "job_id": "review-job-heartbeat",
        "kind": "bestplan_review_resume",
        "plan_id": target.plan_id,
        "profile": "profile-heartbeat",
        "session_id": "session-heartbeat",
        "state_db_path": str(state_db),
        "tracker_path": str((tmp_path / "tracker.json").resolve()),
        "workspace": str(workspace.resolve()),
    }
    result = []
    errors = []

    def run():
        try:
            result.append(delegate_tool.resume_bestplan_review_request(
                request, adapter=BlockingAdapter(),
            ))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(2)
    first = store.get_job("review-job-heartbeat")
    time.sleep(0.15)
    renewed = store.get_job("review-job-heartbeat")
    with pytest.raises(review_engine.ReviewLeaseConflict, match="active owner"):
        store.claim_job(
            job_id="review-job-heartbeat",
            owner_id="second-host",
            now_ns=time.time_ns(),
            lease_duration_ns=60_000_000,
            expected_fencing_token=renewed.fencing_token,
        )
    release.set()
    thread.join(timeout=2)

    assert errors == []
    assert result[0]["result"]["status"] == "checkpoint_advanced"
    assert renewed.fencing_token == first.fencing_token
    assert renewed.lease_expires_at_ns > first.lease_expires_at_ns


def test_transient_attempt_counter_is_not_sent_to_strict_resume_worker():
    from tools import async_delegation

    pending: queue.Queue = queue.Queue()
    pending.put({
        "kind": "bestplan_review_resume",
        "delegation_id": "d1",
        "_transient_attempt": 3,
    })
    seen = []

    async_delegation.consume_bestplan_review_recoveries(
        pending,
        worker=lambda request: seen.append(request) or {"status": "resumed"},
        max_items=1,
    )

    assert seen == [{"kind": "bestplan_review_resume", "delegation_id": "d1"}]


def test_recovery_consumer_chains_durable_checkpoints_until_completion(
    monkeypatch,
):
    from tools import async_delegation

    pending: queue.Queue = queue.Queue()
    request = {
        "kind": "bestplan_review_resume",
        "delegation_id": "d1",
    }
    pending.put(request)
    calls = []
    completions = []
    monkeypatch.setattr(
        async_delegation,
        "_complete_bestplan_review_recovery",
        lambda exact_request, completion: (
            completions.append((dict(exact_request), dict(completion))) or True
        ),
    )

    def worker(received):
        calls.append(received)
        if len(calls) == 1:
            return {
                "status": "resumed",
                "result": {"status": "checkpoint_advanced"},
            }
        return {
            "status": "resumed",
            "result": {
                "status": "completed",
                "completion": {"results": []},
            },
        }

    consumed = async_delegation.consume_bestplan_review_recoveries(
        pending,
        worker=worker,
        max_items=2,
    )

    assert consumed == {"consumed": 2, "completed": 1, "deferred": 0}
    assert calls == [request, request]
    assert completions == [(request, {"results": []})]
    assert pending.empty()


def test_recovery_completion_updates_tracker_and_publishes_exactly_once(
    tmp_path, monkeypatch,
):
    from tools import async_delegation
    from tools.process_registry import process_registry

    tracker = (tmp_path / "tracker.json").resolve()
    record = {
        "delegation_id": "d1",
        "status": "review_requeued",
        "delivery_status": "review_requeued",
        "bestplan_plan_id": "plan-1",
        "bestplan_review_job_id": "job-1",
        "bestplan_local_execution": True,
        "bestplan_state_db_path": str((tmp_path / "state.db").resolve()),
        "origin_tracker_path": str(tracker),
        "origin_session_id": "session-1",
        "origin_profile": "default",
        "goal": "finish bestplan",
        "goals": ["finish bestplan"],
        "dispatched_at": time.time() - 1,
        "is_batch": True,
    }
    tracker.write_text(json.dumps({
        "version": 1,
        "records": {"d1": {
            "delegation_id": "d1",
            "record": record,
            "status": "review_requeued",
            "delivery_status": "review_requeued",
        }},
    }), encoding="utf-8")
    completion_queue: queue.Queue = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", completion_queue)
    marked = []
    monkeypatch.setattr(
        async_delegation,
        "_mark_bestplan_completed_unverified",
        lambda persisted, event: marked.append((persisted, event)) or True,
    )
    request = {
        "kind": "bestplan_review_resume",
        "delegation_id": "d1",
        "job_id": "job-1",
        "tracker_path": str(tracker),
    }
    completion = {
        "results": [{"status": "frozen", "summary": "landed"}],
        "local_main_oid": "a" * 40,
        "push_pending": True,
    }

    def finish(_request):
        return {
            "status": "resumed",
            "result": {"status": "completed", "completion": completion},
        }

    first = queue.Queue()
    first.put(request)
    assert async_delegation.consume_bestplan_review_recoveries(
        first, worker=finish, max_items=1,
    )["completed"] == 1
    persisted = json.loads(tracker.read_text(encoding="utf-8"))
    assert persisted["records"]["d1"]["status"] == "completed"
    event = completion_queue.get_nowait()
    assert event["status"] == "completed"
    assert event["results"] == completion["results"]
    assert len(marked) == 1

    duplicate = queue.Queue()
    duplicate.put(request)
    async_delegation.consume_bestplan_review_recoveries(
        duplicate, worker=finish, max_items=1,
    )
    assert completion_queue.empty()
    assert len(marked) == 1


def test_restart_requeues_a_persisted_review_requeued_crash_window(
    tmp_path,
):
    from tools import async_delegation

    tracker = (tmp_path / "requeued.json").resolve()
    state_db = (tmp_path / "requeued.db").resolve()
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    process = multiprocessing.get_context("spawn").Process(
        target=_seed_recovery_process,
        args=(
            str(state_db),
            str(tracker),
            str(workspace),
            "slot_1",
            "child-provider-secret-must-not-persist",
        ),
    )
    process.start()
    process.join(timeout=20)
    assert process.exitcode == 0
    first_queue: queue.Queue = queue.Queue()
    async_delegation.recover_async_delegations(
        tracker,
        target_queue=queue.Queue(),
        review_recovery_queue=first_queue,
    )
    assert first_queue.get_nowait()["job_id"] == "review-job-recovery"

    # Simulate a crash after review_requeued was stored but before consumption.
    with async_delegation._records_lock:
        async_delegation._records.clear()
    second_queue: queue.Queue = queue.Queue()
    recovered = async_delegation.recover_async_delegations(
        tracker,
        target_queue=queue.Queue(),
        review_recovery_queue=second_queue,
    )

    assert recovered["review_requeued"] == 1
    assert second_queue.get_nowait()["job_id"] == "review-job-recovery"
    assert second_queue.empty()
    live = {
        item["delegation_id"]: item
        for item in async_delegation.list_async_delegations()
    }
    assert live["delegation-slot_1"]["status"] == "review_requeued"
    assert async_delegation.interrupt_all(reason="operator_stop") == 1


def test_production_recovery_adapter_resolves_exact_live_reviewer_routes(
    tmp_path, monkeypatch,
):
    from agent import review_engine
    from tools import delegate_tool

    target = _target(review_engine)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_db = tmp_path / "state.db"
    candidate_routes = [
        {
            "route": "candidate-0",
            "provider": "novita",
            "model": "deepseek/recover",
            "runtime_fingerprint": "d" * 64,
            "toolsets": ["file"],
            "bestplan_toolsets": ["file"],
        },
    ]
    manifest = {
        "version": 1,
        "mode": "delegate",
        "risk": "high",
        "slices": [{
            "id": "repair-slice",
            "kind": "implement",
            "goal": "Repair the approved slice",
            "depends_on": [],
            "capability": "fast_fallback",
            "workspace": str(workspace.resolve()),
            "allowed_paths": ["src/"],
            "read_only": False,
            "expected_artifacts": ["src/result.py"],
            "acceptance": ["The repair passes checks"],
        }],
        "merge_policy": "apply independent candidates in manifest order",
        "stop_condition": "all acceptance conditions pass",
        "escalation_predicates": ["review_blocker"],
    }
    review_engine.ReviewStore(state_db).create_job(
        job_id="review-job-recovery",
        source_kind=target.source_kind,
        source_id=target.plan_id,
        target_digest=target.target_digest,
        policy_digest=target.policy_digest,
        integration_oid=target.integration_oid,
        check_receipt_digest=target.check_receipt_digest,
        adapter_version="local-bestplan.v1",
        owner_session_id="session-recovery",
        owner_profile="profile-recovery",
        workspace=str(workspace.resolve()),
        adapter_state={
            "schema": "hermes.bestplan.local-review-adapter.v1",
            "manifest": manifest,
        },
        runtime_routes=[*candidate_routes, *_review_routes()],
    )
    resolved_reviewers = _review_routes("live-provider-secret")
    resolved_candidates = [{
        **candidate_routes[0],
        "route": "code_worker",
        "api_key": "live-provider-secret",
    }]

    def resolve(tasks, parent_agent, **kwargs):
        del parent_agent
        if tasks[0]["route"] == "smart_reviewer":
            return resolved_reviewers
        assert kwargs["expected"][0]["runtime_fingerprint"] == "d" * 64
        return resolved_candidates

    monkeypatch.setattr(
        delegate_tool,
        "resolve_bestplan_runtime_specs",
        resolve,
    )
    monkeypatch.setattr(
        "agent.bestplan_local.build_local_review_authority_bindings",
        lambda routes: tuple(
            SimpleNamespace(
                slot=item["route"],
                provider=item["provider"],
                model=item["model"],
                runtime_fingerprint=item["runtime_fingerprint"],
                authority=object(),
            )
            for item in routes
        ),
    )
    job = review_engine.ReviewStore(state_db).get_job("review-job-recovery")
    adapter = delegate_tool.LocalBestplanReviewRecoveryAdapter()

    routes = adapter.resolve_runtime_routes(
        job=job,
        request={"workspace": str(workspace.resolve())},
    )

    assert routes == [
        {**resolved_candidates[0], "route": "candidate-0"},
        *resolved_reviewers,
    ]
    assert "live-provider-secret" in json.dumps(routes)
    assert "live-provider-secret" not in job.runtime_routes_json


def test_recovery_runtime_uses_a_fresh_private_root_for_each_fencing_claim(
    tmp_path, monkeypatch,
):
    from agent import bestplan_local, bestplan_state, review_engine
    from tools import delegate_tool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_db = (tmp_path / "state.db").resolve()
    target = _target(review_engine)
    store = review_engine.ReviewStore(state_db)
    store.create_job(
        job_id="review-job-runtime-root",
        source_kind=target.source_kind,
        source_id=target.plan_id,
        target_digest=target.target_digest,
        policy_digest=target.policy_digest,
        integration_oid=target.integration_oid,
        check_receipt_digest=target.check_receipt_digest,
        adapter_version="local-bestplan.v1",
        owner_session_id="session-runtime-root",
        owner_profile="default",
        workspace=str(workspace.resolve()),
        adapter_state={"schema": "runtime-root-test"},
        runtime_routes=[],
    )
    claim = store.claim_job(
        job_id="review-job-runtime-root",
        owner_id="recovery-owner",
        now_ns=1,
        lease_duration_ns=10_000,
        expected_fencing_token=0,
    )
    observed_plan_ids = []
    runtime = SimpleNamespace(candidate_runtime=object())

    def build_runtime(**kwargs):
        observed_plan_ids.append(kwargs["plan_id"])
        return runtime

    monkeypatch.setattr(
        bestplan_local, "build_local_execution_runtime", build_runtime,
    )
    monkeypatch.setattr(
        delegate_tool,
        "_preflight_bestplan_candidates",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        bestplan_state, "_plan_to_delegate_tasks", lambda *_args, **_kwargs: [],
    )
    adapter = delegate_tool.LocalBestplanReviewRecoveryAdapter()
    adapter._state_db_path = state_db
    adapter._resolved_candidate_runtimes = [{"route": "code_worker"}]
    adapter._candidate_bindings = (SimpleNamespace(authority=object()),)
    plan_context = {
        "validated": SimpleNamespace(
            source_snapshot=object(),
            plan=SimpleNamespace(to_manifest=lambda: {"version": 1}),
            contract={"schema": "test.contract"},
        ),
    }

    adapter._execution_context(job=claim, plan_context=plan_context)

    assert observed_plan_ids == [delegate_tool._bestplan_safe_identifier(
        "review-runtime",
        target.plan_id,
        "review-job-runtime-root",
        claim.fencing_token,
    )]
    assert observed_plan_ids[0] != target.plan_id


def test_default_recovery_worker_uses_only_the_owning_profile_scope(
    tmp_path, monkeypatch,
):
    from agent import secret_scope
    from hermes_constants import get_hermes_home
    from tools import async_delegation, delegate_tool

    hermes_root = tmp_path / "hermes"
    profile_home = hermes_root / "profiles" / "secondary"
    profile_home.mkdir(parents=True)
    (hermes_root / ".env").write_text(
        "ANTHROPIC_API_KEY=default-profile-secret\n", encoding="utf-8",
    )
    (profile_home / ".env").write_text(
        "ANTHROPIC_API_KEY=secondary-profile-secret\n", encoding="utf-8",
    )
    (profile_home / "state.db").touch()
    monkeypatch.setenv("HERMES_HOME", str(hermes_root))

    observed = {}

    class Adapter:
        def bind_cancel_event(self, _event):
            return None

    monkeypatch.setattr(
        delegate_tool, "LocalBestplanReviewRecoveryAdapter", Adapter,
    )

    def resume(request, *, adapter):
        del request, adapter
        observed["home"] = get_hermes_home().resolve()
        observed["secret"] = secret_scope.get_secret("ANTHROPIC_API_KEY")
        return {"status": "resumed"}

    monkeypatch.setattr(
        delegate_tool, "resume_bestplan_review_request", resume,
    )
    secret_scope.set_multiplex_active(True)
    try:
        result = async_delegation._default_bestplan_review_recovery_worker({
            "profile": "secondary",
            "kind": "bestplan_review_resume",
            "state_db_path": str((profile_home / "state.db").resolve()),
        })
    finally:
        secret_scope.set_multiplex_active(False)

    assert result == {"status": "resumed"}
    assert observed == {
        "home": profile_home.resolve(),
        "secret": "secondary-profile-secret",
    }
    assert secret_scope.current_secret_scope() is None
