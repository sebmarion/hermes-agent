from __future__ import annotations

import json
import os
import queue
import sqlite3
import time
from pathlib import Path

import pytest

from agent.review_engine import ReviewLeaseConflict, ReviewStore
from tests.agent.test_bestplan_landing_gate import (
    _claim_landing,
    _prepare_landing,
)
from tools import async_delegation as ad
from tools.process_registry import _format_async_delegation


@pytest.fixture(autouse=True)
def _isolated_async_records(monkeypatch, tmp_path):
    tracker = tmp_path / "async-delegations.json"

    monkeypatch.setattr(
        ReviewStore,
        "_lease_now_ns",
        lambda _self: 0,
    )

    def persistence_path(explicit=None):
        return Path(explicit).resolve() if explicit else tracker

    monkeypatch.setattr(ad, "_persistence_path", persistence_path)
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


def _register_local_bestplan(
    *,
    state_db_path: Path,
    review_job_id: str,
    interrupt_fn,
    delegation_id: str,
    status: str = "running",
    origin_ui_session_id: str = "",
):
    now = time.time()
    record = {
        "delegation_id": delegation_id,
        "goal": "BestPlan review interrupt probe",
        "goals": ["implement exact change"],
        "session_key": "session-1",
        "origin_ui_session_id": origin_ui_session_id,
        "status": status,
        "delivery_status": status,
        "dispatched_at": now,
        "last_heartbeat_at": now,
        "interrupt_fn": interrupt_fn,
        "is_batch": True,
        "owner_pid": os.getpid(),
        "bestplan_plan_id": "bp-local",
        "bestplan_local_execution": True,
        "bestplan_state_db_path": str(state_db_path),
        "bestplan_review_job_id": review_job_id,
    }
    ad._records[delegation_id] = record
    return record


def _claimed_review_job(tmp_path: Path):
    state_db_path = tmp_path / "state.db"
    store = ReviewStore(state_db_path)
    store.create_job(
        job_id="review-job-live",
        source_kind="bestplan_integration",
        source_id="bp-local",
        target_digest="1" * 64,
        policy_digest="2" * 64,
        integration_oid="3" * 40,
        check_receipt_digest="4" * 64,
        adapter_version="test-v1",
        owner_session_id="session-1",
        owner_profile="coder",
        workspace=str(tmp_path.resolve()),
        adapter_state={},
        runtime_routes=[],
    )
    claim = store.claim_job(
        job_id="review-job-live",
        owner_id="review-worker",
        now_ns=time.time_ns(),
        lease_duration_ns=60_000_000_000,
        expected_fencing_token=0,
    )
    return state_db_path, store, claim


def test_production_interrupt_persists_review_cancel_before_signalling_children(
    tmp_path,
):
    state_db_path, store, _claim = _claimed_review_job(tmp_path)
    observed_states: list[str] = []
    record = _register_local_bestplan(
        state_db_path=state_db_path,
        review_job_id="review-job-live",
        delegation_id="deleg-review-live",
        interrupt_fn=lambda: observed_states.append(
            ReviewStore(state_db_path).get_job("review-job-live").state
        ),
    )

    assert ad.interrupt_all(reason="operator_stop") == 1

    durable = store.get_job("review-job-live")
    assert observed_states == ["cancel_requested"]
    assert durable.state == "cancel_requested"
    assert durable.cancel_requested is True
    assert record["status"] == "interrupting"


def test_production_interrupt_claims_an_unowned_review_before_cancelling(tmp_path):
    state_db_path = tmp_path / "state.db"
    store = ReviewStore(state_db_path)
    store.create_job(
        job_id="review-job-unowned",
        source_kind="bestplan_integration",
        source_id="bp-local",
        target_digest="1" * 64,
        policy_digest="2" * 64,
        integration_oid="3" * 40,
        check_receipt_digest="4" * 64,
        adapter_version="test-v1",
        owner_session_id="session-1",
        owner_profile="coder",
        workspace=str(tmp_path.resolve()),
        adapter_state={},
        runtime_routes=[],
    )
    observed_states: list[str] = []
    _register_local_bestplan(
        state_db_path=state_db_path,
        review_job_id="review-job-unowned",
        delegation_id="deleg-review-unowned",
        interrupt_fn=lambda: observed_states.append(
            ReviewStore(state_db_path).get_job("review-job-unowned").state
        ),
    )

    assert ad.interrupt_all(reason="operator_stop") == 1

    durable = store.get_job("review-job-unowned")
    assert observed_states == ["cancel_requested"]
    assert durable.cancel_requested is True
    assert durable.fencing_token == 1
    assert durable.owner_id is not None


def test_production_interrupt_rejects_a_mismatched_review_job_identity(tmp_path):
    state_db_path = tmp_path / "state.db"
    store = ReviewStore(state_db_path)
    store.create_job(
        job_id="review-job-wrong-plan",
        source_kind="bestplan_integration",
        source_id="bp-other",
        target_digest="1" * 64,
        policy_digest="2" * 64,
        integration_oid="3" * 40,
        check_receipt_digest="4" * 64,
        adapter_version="test-v1",
        owner_session_id="session-1",
        owner_profile="coder",
        workspace=str(tmp_path.resolve()),
        adapter_state={},
        runtime_routes=[],
    )
    signalled: list[str] = []
    record = _register_local_bestplan(
        state_db_path=state_db_path,
        review_job_id="review-job-wrong-plan",
        delegation_id="deleg-review-wrong-plan",
        interrupt_fn=lambda: signalled.append("signalled"),
    )

    assert ad.interrupt_all(reason="operator_stop") == 0

    durable = store.get_job("review-job-wrong-plan")
    assert durable.cancel_requested is False
    assert durable.state == "queued"
    assert signalled == []
    assert "identity" in record["interrupt_error"]


def test_production_interrupt_before_landing_claim_wins_durably(tmp_path):
    prepared = _prepare_landing(tmp_path)
    signalled: list[str] = []
    _register_local_bestplan(
        state_db_path=Path(prepared.store.state_db_path),
        review_job_id="review-job-local",
        delegation_id="deleg-before-landing",
        interrupt_fn=lambda: signalled.append("signalled"),
    )

    assert ad.interrupt_all(reason="operator_stop") == 1

    durable = prepared.review_store.get_job("review-job-local")
    assert durable.state == "cancel_requested"
    assert durable.cancel_requested is True
    assert signalled == ["signalled"]
    with pytest.raises(ReviewLeaseConflict, match="cancel"):
        _claim_landing(prepared, operation_id="claim-after-production-interrupt")


@pytest.mark.parametrize("status", ("review_requeued", "review_waiting"))
def test_session_interrupt_cancels_an_active_recovery_before_signalling(
    tmp_path, status,
):
    state_db_path, store, _claim = _claimed_review_job(tmp_path)
    recovery_cancel = __import__("threading").Event()
    record = _register_local_bestplan(
        state_db_path=state_db_path,
        review_job_id="review-job-live",
        delegation_id=f"deleg-active-{status}",
        interrupt_fn=recovery_cancel.set,
        status=status,
        origin_ui_session_id="ui-recovery",
    )

    assert ad.interrupt_for_session(
        origin_ui_session_id="ui-recovery",
        reason="operator_stop",
    ) == 1

    durable = store.get_job("review-job-live")
    assert durable.cancel_requested is True
    assert durable.state == "cancel_requested"
    assert recovery_cancel.is_set()


def test_session_interrupt_keeps_queued_recovery_nonterminal_without_callback(
    tmp_path,
):
    state_db_path, store, _claim = _claimed_review_job(tmp_path)
    delegation_id = "delegation-queued-recovery"
    record = _register_local_bestplan(
        state_db_path=state_db_path,
        review_job_id="review-job-live",
        delegation_id=delegation_id,
        status="review_requeued",
        interrupt_fn=None,
        origin_ui_session_id="ui-durable-wait",
    )

    assert ad.interrupt_for_session(
        origin_ui_session_id="ui-durable-wait",
        reason="user_stop",
    ) == 1

    durable = store.get_job("review-job-live")
    assert durable.state == "cancel_requested"
    assert durable.cancel_requested is True
    assert record["status"] == "interrupting"


def test_session_interrupt_finalizes_wait_without_worker_and_restart_is_idle(
    tmp_path,
):
    state_db_path, store, _claim = _claimed_review_job(tmp_path)
    delegation_id = "delegation-durable-wait"
    record = _register_local_bestplan(
        state_db_path=state_db_path,
        review_job_id="review-job-live",
        delegation_id=delegation_id,
        status="review_waiting",
        interrupt_fn=None,
        origin_ui_session_id="ui-durable-wait",
    )
    assert ad._persist_record(record, delivery_status="review_waiting")

    assert ad.interrupt_for_session(
        origin_ui_session_id="ui-durable-wait",
        reason="user_stop",
    ) == 1

    durable = store.get_job("review-job-live")
    assert durable.state == "cancelled"
    assert durable.cancel_requested is True
    assert record["status"] == "interrupted"
    persisted = json.loads(
        (tmp_path / "async-delegations.json").read_text(encoding="utf-8")
    )["records"][delegation_id]
    assert persisted["status"] == "interrupted"
    assert persisted["record"]["status"] == "interrupted"
    assert ad.mark_completion_delivered(delegation_id)

    with ad._records_lock:
        ad._records.clear()
    completion_queue: queue.Queue = queue.Queue()
    recovery_queue: queue.Queue = queue.Queue()
    recovered = ad.recover_async_delegations(
        tmp_path / "async-delegations.json",
        target_queue=completion_queue,
        review_recovery_queue=recovery_queue,
    )
    assert recovered == {"queued": 0, "lost": 0}
    assert completion_queue.empty()
    assert recovery_queue.empty()


def test_cancelled_active_recovery_is_never_scheduled_again(
    tmp_path, monkeypatch,
):
    from tools.delegate_tool import BestplanReviewRecoveryDeferred

    state_db_path, store, _claim = _claimed_review_job(tmp_path)
    delegation_id = "deleg-cancelled-active-recovery"
    record = _register_local_bestplan(
        state_db_path=state_db_path,
        review_job_id="review-job-live",
        delegation_id=delegation_id,
        interrupt_fn=lambda: None,
        status="review_requeued",
        origin_ui_session_id="ui-recovery-race",
    )
    assert ad._persist_record(record, delivery_status="review_requeued")
    monkeypatch.setattr(
        ad,
        "_mark_bestplan_completed_unverified",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        ad,
        "_mark_bestplan_cancelled_terminal",
        lambda *_args, **_kwargs: True,
    )
    scheduled: list[tuple[dict[str, object], str]] = []
    monkeypatch.setattr(
        ad,
        "_schedule_bestplan_review_recovery_retry",
        lambda request, *, reason_code: scheduled.append(
            (dict(request), reason_code)
        ),
    )
    pending: queue.Queue = queue.Queue()
    request = {
        "kind": "bestplan_review_resume",
        "delegation_id": delegation_id,
        "job_id": "review-job-live",
        "tracker_path": str((tmp_path / "async-delegations.json").resolve()),
    }
    pending.put(request)

    def worker(_request, *, cancel_event):
        assert ad.interrupt_for_session(
            origin_ui_session_id="ui-recovery-race",
            reason="operator_stop",
        ) == 1
        assert cancel_event.is_set()
        # A claim racing with durable cancellation currently reaches the
        # generic lease-conflict mapping. The consumer must still consult the
        # durable cancellation bit before it schedules another attempt.
        raise BestplanReviewRecoveryDeferred("review_lease_active")

    consumed = ad.consume_bestplan_review_recoveries(
        pending,
        worker=worker,
        max_items=1,
    )

    durable = store.get_job("review-job-live")
    assert consumed == {"consumed": 1, "completed": 0, "deferred": 0}
    assert durable.cancel_requested is True
    assert durable.state == "cancelled"
    assert scheduled == []
    persisted = json.loads(
        (tmp_path / "async-delegations.json").read_text(encoding="utf-8")
    )["records"][delegation_id]
    assert persisted["status"] == "interrupted"
    assert persisted["record"]["status"] == "interrupted"


def test_cancel_finalize_failure_retries_without_resuming_model_work(
    tmp_path, monkeypatch,
):
    from tools.delegate_tool import BestplanReviewRecoveryDeferred

    state_db_path, store, claim = _claimed_review_job(tmp_path)
    delegation_id = "deleg-cancel-finalize-retry"
    record = _register_local_bestplan(
        state_db_path=state_db_path,
        review_job_id="review-job-live",
        delegation_id=delegation_id,
        interrupt_fn=lambda: None,
        status="review_requeued",
        origin_ui_session_id="ui-cancel-finalize-retry",
    )
    assert ad._persist_record(record, delivery_status="review_requeued")
    monkeypatch.setattr(
        ad,
        "_mark_bestplan_completed_unverified",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        ad,
        "_mark_bestplan_cancelled_terminal",
        lambda *_args, **_kwargs: True,
    )
    tracker = (tmp_path / "async-delegations.json").resolve()
    request = {
        "kind": "bestplan_review_resume",
        "delegation_id": delegation_id,
        "job_id": "review-job-live",
        "plan_id": "bp-local",
        "state_db_path": str(state_db_path.resolve()),
        "tracker_path": str(tracker),
    }
    pending: queue.Queue = queue.Queue()
    pending.put(dict(request))
    worker_calls: list[str] = []
    scheduled: list[tuple[dict[str, object], str]] = []
    real_finalize = ad._finalize_durable_review_cancel
    finalize_calls = 0

    def finalize(cancelled_record):
        nonlocal finalize_calls
        finalize_calls += 1
        if finalize_calls == 1:
            return False
        return real_finalize(cancelled_record)

    def schedule(retry, *, reason_code):
        scheduled.append((dict(retry), reason_code))
        pending.put(dict(retry))

    monkeypatch.setattr(ad, "_finalize_durable_review_cancel", finalize)
    monkeypatch.setattr(ad, "_schedule_bestplan_review_recovery_retry", schedule)

    def worker(_request, *, cancel_event):
        worker_calls.append("worker")
        store.request_cancel(
            job_id="review-job-live",
            owner_id=claim.owner_id,
            fencing_token=claim.fencing_token,
            operation_id="cancel-before-finalize-retry",
            signal_children=cancel_event.set,
        )
        raise BestplanReviewRecoveryDeferred("review_cancelled")

    first = ad.consume_bestplan_review_recoveries(
        pending, worker=worker, max_items=1,
    )
    second = ad.consume_bestplan_review_recoveries(
        pending, worker=worker, max_items=1,
    )

    assert first == {"consumed": 1, "completed": 0, "deferred": 1}
    assert second == {"consumed": 1, "completed": 0, "deferred": 0}
    assert worker_calls == ["worker"]
    assert scheduled[0][1] == "cancel_finalize_failed"
    assert scheduled[0][0]["_cancel_finalize_only"] is True
    assert store.get_job("review-job-live").state == "cancelled"
    persisted = json.loads(tracker.read_text(encoding="utf-8"))["records"][
        delegation_id
    ]
    assert persisted["status"] == "interrupted"
    assert persisted["record"]["status"] == "interrupted"


def test_scheduled_pre_review_interrupt_cancels_pipeline_before_terminal_tracker(
    tmp_path, monkeypatch,
):
    from tests.tools.test_bestplan_review_recovery import (
        _seed_pre_review_execution,
    )

    seeded = _seed_pre_review_execution(tmp_path, monkeypatch)
    pipeline = seeded.review_store.get_execution_pipeline(seeded.plan_id)
    seeded.review_store.release_execution_attempt(
        seeded.plan_id,
        owner_pid=pipeline.attempt_owner_pid,
        owner_process_start_id=pipeline.attempt_owner_process_start_id,
    )
    payload = json.loads(seeded.tracker.read_text(encoding="utf-8"))
    entry = payload["records"][seeded.request["delegation_id"]]
    entry["status"] = "scheduled"
    entry["delivery_status"] = "scheduled"
    entry["record"]["status"] = "scheduled"
    entry["record"]["delivery_status"] = "scheduled"
    entry["record"]["origin_ui_session_id"] = "ui-scheduled-cancel"
    seeded.tracker.write_text(json.dumps(payload), encoding="utf-8")

    signal_calls: list[str] = []
    live = dict(entry["record"])
    live["interrupt_fn"] = lambda: signal_calls.append("unexpected")
    ad._records[live["delegation_id"]] = live

    assert ad.interrupt_for_session(
        origin_ui_session_id="ui-scheduled-cancel",
        reason="operator_stop",
    ) == 1

    assert signal_calls == []
    assert live["status"] == "interrupted"
    pipeline = seeded.review_store.get_execution_pipeline(seeded.plan_id)
    assert pipeline.cancel_requested is True
    assert pipeline.state == "cancelled"
    persisted = json.loads(seeded.tracker.read_text(encoding="utf-8"))[
        "records"
    ][seeded.request["delegation_id"]]
    assert persisted["status"] == "interrupted"
    assert persisted["record"]["status"] == "interrupted"
    assert ad.mark_completion_delivered(live["delegation_id"])

    with ad._records_lock:
        ad._records.clear()
    completion_queue: queue.Queue = queue.Queue()
    recovery_queue: queue.Queue = queue.Queue()
    recovered = ad.recover_async_delegations(
        seeded.tracker,
        target_queue=completion_queue,
        review_recovery_queue=recovery_queue,
    )
    assert recovered == {"queued": 0, "lost": 0}
    assert completion_queue.empty()
    assert recovery_queue.empty()


def test_pre_review_interrupt_is_durable_and_restart_never_resumes_execution(
    tmp_path, monkeypatch,
):
    from agent.bestplan_state import PlanState
    from tests.tools.test_bestplan_review_recovery import (
        _seed_pre_review_execution,
    )
    from tools.delegate_tool import (
        BestplanReviewRecoveryDeferred,
        resume_bestplan_execution_request,
    )

    seeded = _seed_pre_review_execution(tmp_path, monkeypatch)
    payload = json.loads(seeded.tracker.read_text(encoding="utf-8"))
    entry = payload["records"][seeded.request["delegation_id"]]
    entry["status"] = "running"
    entry["delivery_status"] = "running"
    entry["record"]["status"] = "running"
    entry["record"]["delivery_status"] = "running"
    entry["record"]["origin_ui_session_id"] = "ui-pre-review-cancel"
    seeded.tracker.write_text(json.dumps(payload), encoding="utf-8")

    signal_observations: list[tuple[bool, str]] = []
    live = dict(entry["record"])
    live["interrupt_fn"] = lambda: signal_observations.append((
        seeded.review_store.get_execution_pipeline(
            seeded.plan_id,
        ).cancel_requested,
        seeded.review_store.get_execution_pipeline(seeded.plan_id).state,
    ))
    ad._records[live["delegation_id"]] = live

    assert ad._interrupt_records(
        [live], reason="operator_stop", source="test",
    ) == 1
    assert signal_observations == [(True, "cancel_requested")]
    pipeline = seeded.review_store.get_execution_pipeline(seeded.plan_id)
    assert pipeline.cancel_requested is True
    assert pipeline.state == "cancel_requested"

    with ad._records_lock:
        ad._records.clear()
    recovery_queue: queue.Queue = queue.Queue()
    recovered = ad.recover_async_delegations(
        seeded.tracker,
        target_queue=queue.Queue(),
        review_recovery_queue=recovery_queue,
    )
    assert recovered == {"queued": 0, "lost": 0}
    assert recovery_queue.empty()
    pipeline = seeded.review_store.get_execution_pipeline(seeded.plan_id)
    assert pipeline.cancel_requested is True
    assert pipeline.state == "cancelled"
    persisted = json.loads(seeded.tracker.read_text(encoding="utf-8"))[
        "records"
    ][seeded.request["delegation_id"]]
    assert persisted["status"] == "interrupted"
    assert persisted["record"]["status"] == "interrupted"
    plan = seeded.plan_store.get_plan(seeded.plan_id)
    assert plan["state"] == PlanState.FAILED
    assert plan["dispatch_state"] == "terminal"

    with pytest.raises(BestplanReviewRecoveryDeferred) as rejected:
        resume_bestplan_execution_request(seeded.request)
    assert rejected.value.code == "execution_cancelled"


def test_restart_cancel_without_canonical_plan_retries_model_free(tmp_path):
    state_db_path, store, claim = _claimed_review_job(tmp_path)
    store.request_cancel(
        job_id="review-job-live",
        owner_id=claim.owner_id,
        fencing_token=claim.fencing_token,
        operation_id="cancel-before-crash",
        signal_children=lambda: None,
    )
    tracker = tmp_path / "async-delegations.json"
    record = {
        "delegation_id": "deleg-cancelled-crash",
        "goal": "BestPlan cancelled before process loss",
        "goals": ["implement exact change"],
        "session_key": "session-1",
        "origin_session_id": "session-1",
        "origin_profile": "coder",
        "status": "interrupting",
        "delivery_status": "interrupting",
        "dispatched_at": time.time() - 30,
        "last_heartbeat_at": time.time() - 30,
        "is_batch": True,
        "owner_pid": 999_999,
        "bestplan_plan_id": "bp-local",
        "bestplan_local_execution": True,
        "bestplan_state_db_path": str(state_db_path.resolve()),
        "bestplan_review_job_id": "review-job-live",
    }
    tracker.write_text(
        json.dumps(
            {
                "version": 1,
                "records": {
                    record["delegation_id"]: {
                        "delegation_id": record["delegation_id"],
                        "record": record,
                        "status": "interrupting",
                        "delivery_status": "interrupting",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    recovery_queue: queue.Queue = queue.Queue()
    completion_queue: queue.Queue = queue.Queue()

    recovered = ad.recover_async_delegations(
        tracker,
        target_queue=completion_queue,
        review_recovery_queue=recovery_queue,
    )

    persisted = json.loads(tracker.read_text(encoding="utf-8"))["records"][
        "deleg-cancelled-crash"
    ]
    assert recovered == {"queued": 0, "lost": 0, "review_requeued": 1}
    retry = recovery_queue.get_nowait()
    assert retry["_cancel_finalize_only"] is True
    assert recovery_queue.empty()
    assert completion_queue.empty()
    assert persisted["status"] == "review_requeued"
    assert persisted["record"]["status"] == "review_requeued"
    assert store.get_job("review-job-live").state == "cancelled"


@pytest.mark.parametrize("status", ("review_waiting", "interrupting"))
def test_restart_keeps_cancel_nonterminal_when_durable_finalize_fails(
    tmp_path, monkeypatch, status,
):
    state_db_path, store, claim = _claimed_review_job(tmp_path)
    store.request_cancel(
        job_id="review-job-live",
        owner_id=claim.owner_id,
        fencing_token=claim.fencing_token,
        operation_id=f"cancel-before-restart-{status}",
        signal_children=lambda: None,
    )
    tracker = tmp_path / "async-delegations.json"
    record = {
        "delegation_id": f"deleg-finalize-fails-{status}",
        "goal": "Retry exact durable cancellation",
        "goals": ["Retry exact durable cancellation"],
        "session_key": "session-1",
        "origin_session_id": "session-1",
        "origin_profile": "coder",
        "origin_tracker_path": str(tracker.resolve()),
        "status": status,
        "delivery_status": status,
        "dispatched_at": time.time() - 30,
        "last_heartbeat_at": time.time() - 30,
        "is_batch": True,
        "owner_pid": 999_999,
        "bestplan_plan_id": "bp-local",
        "bestplan_local_execution": True,
        "bestplan_state_db_path": str(state_db_path.resolve()),
        "bestplan_review_job_id": "review-job-live",
    }
    tracker.write_text(json.dumps({
        "version": 1,
        "records": {record["delegation_id"]: {
            "delegation_id": record["delegation_id"],
            "record": record,
            "status": status,
            "delivery_status": status,
        }},
    }), encoding="utf-8")
    recovery_queue: queue.Queue = queue.Queue()
    monkeypatch.setattr(
        ad, "_finalize_durable_review_cancel", lambda _record: False,
    )

    recovered = ad.recover_async_delegations(
        tracker,
        target_queue=queue.Queue(),
        review_recovery_queue=recovery_queue,
    )

    assert recovered == {"queued": 0, "lost": 0, "review_requeued": 1}
    retry = recovery_queue.get_nowait()
    assert retry["delegation_id"] == record["delegation_id"]
    persisted = json.loads(tracker.read_text(encoding="utf-8"))["records"][
        record["delegation_id"]
    ]
    assert persisted["status"] == "review_requeued"
    assert persisted["record"]["status"] == "review_requeued"
    assert "completed_at" not in persisted["record"]
    assert store.get_job("review-job-live").state == "cancel_requested"


@pytest.mark.parametrize("status", ("running", "interrupting"))
def test_restart_cancel_finalize_failure_without_resume_stays_nonterminal(
    tmp_path, monkeypatch, status,
):
    state_db_path, store, claim = _claimed_review_job(tmp_path)
    store.request_cancel(
        job_id="review-job-live",
        owner_id=claim.owner_id,
        fencing_token=claim.fencing_token,
        operation_id=f"cancel-without-resume-{status}",
        signal_children=lambda: None,
    )
    tracker = tmp_path / "async-delegations.json"
    delegation_id = f"deleg-cancel-without-resume-{status}"
    record = {
        "delegation_id": delegation_id,
        "goal": "Retry durable cancellation only",
        "goals": ["Retry durable cancellation only"],
        "session_key": "session-1",
        "origin_session_id": "session-1",
        "origin_profile": "coder",
        "origin_tracker_path": str(tracker.resolve()),
        "status": status,
        "delivery_status": status,
        "dispatched_at": time.time() - 30,
        "last_heartbeat_at": time.time() - 30,
        "is_batch": True,
        "owner_pid": 999_999,
        "owner_started_at": 1,
        "bestplan_plan_id": "bp-local",
        "bestplan_local_execution": True,
        "bestplan_state_db_path": str(state_db_path.resolve()),
        "bestplan_review_job_id": "review-job-live",
    }
    tracker.write_text(json.dumps({
        "version": 1,
        "records": {delegation_id: {
            "delegation_id": delegation_id,
            "record": record,
            "status": status,
            "delivery_status": status,
        }},
    }), encoding="utf-8")
    monkeypatch.setattr(ad, "_owner_liveness", lambda _record: False)
    monkeypatch.setattr(
        ad, "_finalize_durable_review_cancel", lambda _record: False,
    )
    monkeypatch.setattr(
        ad, "_bestplan_review_resume_request", lambda *_args, **_kwargs: None,
    )
    recovery_queue: queue.Queue = queue.Queue()
    completion_queue: queue.Queue = queue.Queue()

    recovered = ad.recover_async_delegations(
        tracker,
        target_queue=completion_queue,
        review_recovery_queue=recovery_queue,
    )

    assert recovered == {"queued": 0, "lost": 0, "review_waiting": 1}
    assert recovery_queue.empty()
    assert completion_queue.empty()
    persisted = json.loads(tracker.read_text(encoding="utf-8"))["records"][
        delegation_id
    ]
    assert persisted["status"] == "review_waiting"
    assert persisted["record"]["status"] == "review_waiting"
    assert persisted["record"]["review_recovery_reason_code"] == (
        "cancel_finalize_failed"
    )
    assert "event" not in persisted
    assert "result" not in persisted
    assert store.get_job("review-job-live").state == "cancel_requested"


@pytest.mark.parametrize("status", ("review_waiting", "review_requeued"))
def test_durable_review_wait_survives_cleanup_and_is_idempotently_active(
    tmp_path, monkeypatch, status,
):
    tracker = (tmp_path / "durable-wait.json").resolve()
    record = {
        "delegation_id": f"deleg-durable-{status}",
        "goal": "resume the durable BestPlan review",
        "goals": ["resume the durable BestPlan review"],
        "status": status,
        "delivery_status": status,
        "dispatched_at": 1.0,
        "last_heartbeat_at": 1.0,
        "is_batch": True,
        "bestplan_plan_id": "bp-local",
        "bestplan_local_execution": True,
    }
    data = {
        "version": 1,
        "records": {
            record["delegation_id"]: {
                "delegation_id": record["delegation_id"],
                "record": dict(record),
                "status": status,
                "delivery_status": status,
            }
        },
    }
    monkeypatch.setattr(
        ad,
        "_retention_policy_from_config",
        lambda: {
            "completed_seconds": 1,
            "failed_seconds": 1,
            "lost_seconds": 1,
            "max_bytes": 1,
        },
    )

    assert ad._cleanup_persisted_data_locked(data, now=10_000) == 0
    assert set(data["records"]) == {record["delegation_id"]}
    tracker.write_text(json.dumps(data), encoding="utf-8")

    replay = ad.dispatch_async_delegation_batch(
        goals=["must not dispatch a second worker"],
        context=None,
        toolsets=None,
        role="leaf",
        model="test",
        session_key="session-1",
        runner=lambda: pytest.fail("idempotent wait replay started a worker"),
        max_async_children=1,
        delegation_id=record["delegation_id"],
        origin_tracker_path=str(tracker),
    )

    assert replay == {
        "status": "dispatched",
        "delegation_id": record["delegation_id"],
        "phase": status,
        "idempotent_replay": True,
    }
    # A durable wait owns no live worker slot. It must not reject unrelated
    # work at a capacity of one.
    ad._records[record["delegation_id"]] = dict(record)
    release = __import__("threading").Event()

    def run_independent():
        release.wait(1)
        return {"results": [{"status": "completed"}]}

    admitted = ad.dispatch_async_delegation_batch(
        goals=["independent work"],
        context=None,
        toolsets=None,
        role="leaf",
        model="test",
        session_key="session-2",
        runner=run_independent,
        max_async_children=1,
        delegation_id=f"deleg-independent-{status}",
    )
    release.set()
    assert admitted["status"] == "dispatched"


def test_nested_authority_block_moves_tracker_to_review_waiting(
    tmp_path, monkeypatch,
):
    tracker = (tmp_path / "authority-wait.json").resolve()
    delegation_id = "deleg-authority-wait"
    record = {
        "delegation_id": delegation_id,
        "goal": "repair only with exact authority",
        "goals": ["repair only with exact authority"],
        "status": "review_requeued",
        "delivery_status": "review_requeued",
        "dispatched_at": time.time(),
        "last_heartbeat_at": time.time(),
        "is_batch": True,
        "bestplan_plan_id": "bp-local",
        "bestplan_review_job_id": "review-job-authority",
        "bestplan_local_execution": True,
    }
    tracker.write_text(
        json.dumps({
            "version": 1,
            "records": {delegation_id: {
                "delegation_id": delegation_id,
                "record": record,
                "status": "review_requeued",
                "delivery_status": "review_requeued",
            }},
        }),
        encoding="utf-8",
    )
    ad._records[delegation_id] = dict(record)
    retries: list[object] = []
    monkeypatch.setattr(
        ad,
        "_schedule_bestplan_review_recovery_retry",
        lambda *args, **kwargs: retries.append((args, kwargs)),
    )
    pending: queue.Queue = queue.Queue()
    request = {
        "kind": "bestplan_review_resume",
        "delegation_id": delegation_id,
        "job_id": "review-job-authority",
        "tracker_path": str(tracker),
    }
    pending.put(request)

    consumed = ad.consume_bestplan_review_recoveries(
        pending,
        worker=lambda _request, **_kwargs: {
            "status": "resumed",
            "result": {"status": "blocked_requires_authority"},
        },
        max_items=1,
    )

    persisted = json.loads(tracker.read_text(encoding="utf-8"))["records"][
        delegation_id
    ]
    assert consumed == {"consumed": 1, "completed": 0, "deferred": 1}
    assert persisted["status"] == "review_waiting"
    assert persisted["record"]["status"] == "review_waiting"
    assert persisted["record"]["review_recovery_reason_code"] == (
        "blocked_requires_authority"
    )
    assert pending.empty()
    assert retries == []


def test_stop_during_normal_recovery_result_wins_after_child_extinction(
    tmp_path,
    monkeypatch,
):
    state_db_path, store, _claim = _claimed_review_job(tmp_path)
    tracker = (tmp_path / "cancel-normal-result.json").resolve()
    delegation_id = "deleg-cancel-normal-result"
    record = _register_local_bestplan(
        state_db_path=state_db_path,
        review_job_id="review-job-live",
        delegation_id=delegation_id,
        status="review_requeued",
        interrupt_fn=None,
    )
    record["origin_tracker_path"] = str(tracker)
    assert ad._persist_record(record, delivery_status="review_requeued")
    pending: queue.Queue = queue.Queue()
    request = {
        "kind": "bestplan_review_resume",
        "delegation_id": delegation_id,
        "job_id": "review-job-live",
        "plan_id": "bp-local",
        "state_db_path": str(state_db_path.resolve()),
        "tracker_path": str(tracker),
    }
    pending.put(request)
    monkeypatch.setattr(
        ad,
        "_mark_bestplan_completed_unverified",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        ad,
        "_mark_bestplan_cancelled_terminal",
        lambda *_args, **_kwargs: True,
    )

    def worker(_request, *, cancel_event):
        assert ad.interrupt_all(reason="operator_stop") == 1
        assert cancel_event.is_set()
        return {
            "status": "resumed",
            "result": {"status": "blocked_requires_authority"},
        }

    consumed = ad.consume_bestplan_review_recoveries(
        pending,
        worker=worker,
        max_items=1,
    )

    durable = store.get_job("review-job-live")
    persisted = json.loads(tracker.read_text(encoding="utf-8"))["records"][
        delegation_id
    ]
    assert consumed == {"consumed": 1, "completed": 0, "deferred": 0}
    assert durable.state == "cancelled"
    assert persisted["status"] == "interrupted"
    assert persisted["record"]["status"] == "interrupted"
    assert pending.empty()


def test_live_cancel_bridge_failure_schedules_model_free_terminal_retry(
    tmp_path,
    monkeypatch,
):
    from tests.tools.test_bestplan_review_recovery import (
        _seed_pre_review_execution,
    )

    seeded = _seed_pre_review_execution(tmp_path, monkeypatch)
    pipeline = seeded.review_store.get_execution_pipeline(seeded.plan_id)
    seeded.review_store.request_execution_pipeline_cancel(
        plan_id=seeded.plan_id,
        delegation_id=seeded.request["delegation_id"],
        job_id=seeded.request["job_id"],
    )
    seeded.review_store.finalize_execution_pipeline_cancel(
        plan_id=seeded.plan_id,
        delegation_id=seeded.request["delegation_id"],
        job_id=seeded.request["job_id"],
    )
    live = json.loads(seeded.tracker.read_text(encoding="utf-8"))["records"][
        seeded.request["delegation_id"]
    ]["record"]
    live["status"] = "review_requeued"
    live["delivery_status"] = "review_requeued"
    live["origin_tracker_path"] = str(seeded.tracker)
    ad._records[live["delegation_id"]] = live
    assert ad._persist_record(live, delivery_status="review_requeued")
    pending: queue.Queue = queue.Queue()
    pending.put(dict(seeded.request))
    scheduled: list[dict[str, object]] = []
    monkeypatch.setattr(
        ad,
        "_mark_bestplan_completed_unverified",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        ad,
        "_schedule_bestplan_review_recovery_retry",
        lambda request, **_kwargs: scheduled.append(dict(request)),
    )

    consumed = ad.consume_bestplan_review_recoveries(
        pending,
        worker=lambda _request: pytest.fail("cancel-only retry ran model work"),
        max_items=1,
    )

    current = ad._records[live["delegation_id"]]
    persisted = json.loads(seeded.tracker.read_text(encoding="utf-8"))["records"][
        live["delegation_id"]
    ]
    assert pipeline.state == "pending"
    assert consumed == {"consumed": 1, "completed": 0, "deferred": 1}
    assert current[ad._BESTPLAN_TERMINALIZATION_PENDING] is True
    assert persisted["record"][ad._BESTPLAN_TERMINALIZATION_PENDING] is True
    assert scheduled[0]["_cancel_finalize_only"] is True


@pytest.mark.parametrize(
    "identity_mismatch",
    (
        "delegation_id",
        "job_id",
        "session_id",
        "profile",
        "workspace",
        "state_db_path",
        "tracker_path",
    ),
)
def test_cancelled_execution_pipeline_never_terminalizes_mismatched_tracker(
    tmp_path,
    monkeypatch,
    identity_mismatch,
):
    from tests.tools.test_bestplan_review_recovery import (
        _seed_pre_review_execution,
    )

    seeded = _seed_pre_review_execution(tmp_path, monkeypatch)
    seeded.review_store.request_execution_pipeline_cancel(
        plan_id=seeded.plan_id,
        delegation_id=seeded.request["delegation_id"],
        job_id=seeded.request["job_id"],
    )
    seeded.review_store.finalize_execution_pipeline_cancel(
        plan_id=seeded.plan_id,
        delegation_id=seeded.request["delegation_id"],
        job_id=seeded.request["job_id"],
    )
    payload = json.loads(seeded.tracker.read_text(encoding="utf-8"))
    entry = payload["records"][seeded.request["delegation_id"]]
    record = entry["record"]
    if identity_mismatch == "delegation_id":
        record["delegation_id"] = "delegation-stale"
    elif identity_mismatch == "job_id":
        record["bestplan_review_job_id"] = "review-job-stale"
    elif identity_mismatch == "session_id":
        record["origin_session_id"] = "session-stale"
    elif identity_mismatch == "profile":
        record["origin_profile"] = "profile-stale"
    elif identity_mismatch == "workspace":
        other_workspace = tmp_path / "other-workspace"
        other_workspace.mkdir()
        with sqlite3.connect(seeded.state_db) as connection:
            connection.execute(
                "DROP TRIGGER "
                "trg_bestplan_execution_pipeline_identity_immutable"
            )
            connection.execute(
                "UPDATE bestplan_execution_pipelines SET workspace=? "
                "WHERE plan_id=?",
                (str(other_workspace.resolve()), seeded.plan_id),
            )
    elif identity_mismatch == "state_db_path":
        other_state = (tmp_path / "other-state" / "state.db").resolve()
        other_state.parent.mkdir()
        with sqlite3.connect(seeded.state_db) as source:
            with sqlite3.connect(other_state) as target:
                source.backup(target)
        record["bestplan_state_db_path"] = str(other_state)
    elif identity_mismatch == "tracker_path":
        other_tracker = (
            tmp_path / "other-state" / "async_delegations.json"
        ).resolve()
        other_tracker.parent.mkdir()
        other_tracker.write_text(json.dumps(payload), encoding="utf-8")
        record["origin_tracker_path"] = str(other_tracker)
    seeded.tracker.write_text(json.dumps(payload), encoding="utf-8")
    canonical_before = seeded.plan_store.get_plan(seeded.plan_id)
    pipeline_before = seeded.review_store.get_execution_pipeline(
        seeded.plan_id
    )
    terminal_bridge_calls: list[str] = []
    monkeypatch.setattr(ad, "_owner_liveness", lambda _record: False)
    monkeypatch.setattr(
        ad,
        "_mark_bestplan_cancelled_terminal",
        lambda _record: terminal_bridge_calls.append("called") or True,
    )
    recovery_queue: queue.Queue = queue.Queue()
    completion_queue: queue.Queue = queue.Queue()

    assert ad._durable_review_cancelled(record) is False
    assert ad._finalize_durable_review_cancel(record) is False
    recovered = ad.recover_async_delegations(
        seeded.tracker,
        target_queue=completion_queue,
        review_recovery_queue=recovery_queue,
    )

    persisted = json.loads(seeded.tracker.read_text(encoding="utf-8"))[
        "records"
    ][seeded.request["delegation_id"]]
    assert recovered == {"queued": 0, "lost": 0}
    assert persisted["status"] == "review_requeued"
    assert persisted["record"]["status"] == "review_requeued"
    assert terminal_bridge_calls == []
    assert completion_queue.empty()
    assert recovery_queue.empty()
    assert seeded.plan_store.get_plan(seeded.plan_id) == canonical_before
    assert seeded.review_store.get_execution_pipeline(
        seeded.plan_id
    ) == pipeline_before


def test_cancelled_execution_pipeline_exact_identity_finalizes_idempotently(
    tmp_path,
    monkeypatch,
):
    from tests.tools.test_bestplan_review_recovery import (
        _seed_pre_review_execution,
    )

    seeded = _seed_pre_review_execution(tmp_path, monkeypatch)
    seeded.review_store.request_execution_pipeline_cancel(
        plan_id=seeded.plan_id,
        delegation_id=seeded.request["delegation_id"],
        job_id=seeded.request["job_id"],
    )
    seeded.review_store.finalize_execution_pipeline_cancel(
        plan_id=seeded.plan_id,
        delegation_id=seeded.request["delegation_id"],
        job_id=seeded.request["job_id"],
    )
    record = json.loads(seeded.tracker.read_text(encoding="utf-8"))[
        "records"
    ][seeded.request["delegation_id"]]["record"]

    assert ad._durable_review_cancelled(record) is True
    assert ad._finalize_durable_review_cancel(record) is True
    assert ad._finalize_durable_review_cancel(record) is True


def test_cancelled_execution_pipeline_default_profile_terminalizes_on_restart(
    tmp_path,
    monkeypatch,
):
    from tests.tools.test_bestplan_review_recovery import (
        _seed_pre_review_execution,
    )

    seeded = _seed_pre_review_execution(tmp_path, monkeypatch)
    with sqlite3.connect(seeded.state_db) as connection:
        connection.execute(
            "DROP TRIGGER trg_bestplan_execution_pipeline_identity_immutable"
        )
        approval_trigger = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND sql LIKE '%protocol-2 approval inputs are immutable%'"
        ).fetchone()
        assert approval_trigger is not None
        connection.execute(f'DROP TRIGGER "{approval_trigger[0]}"')
        connection.execute(
            "UPDATE bestplan_execution_pipelines SET owner_profile='' "
            "WHERE plan_id=?",
            (seeded.plan_id,),
        )
        connection.execute(
            "UPDATE bestplan_plans SET profile='' WHERE plan_id=?",
            (seeded.plan_id,),
        )
    seeded.review_store.request_execution_pipeline_cancel(
        plan_id=seeded.plan_id,
        delegation_id=seeded.request["delegation_id"],
        job_id=seeded.request["job_id"],
    )
    seeded.review_store.finalize_execution_pipeline_cancel(
        plan_id=seeded.plan_id,
        delegation_id=seeded.request["delegation_id"],
        job_id=seeded.request["job_id"],
    )
    payload = json.loads(seeded.tracker.read_text(encoding="utf-8"))
    record = payload["records"][seeded.request["delegation_id"]]["record"]
    record["origin_profile"] = ""
    seeded.tracker.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(ad, "_owner_liveness", lambda _record: False)
    recovery_queue: queue.Queue = queue.Queue()

    assert ad._durable_review_cancelled(record) is True
    recovered = ad.recover_async_delegations(
        seeded.tracker,
        target_queue=queue.Queue(),
        review_recovery_queue=recovery_queue,
    )

    persisted = json.loads(seeded.tracker.read_text(encoding="utf-8"))[
        "records"
    ][seeded.request["delegation_id"]]
    assert recovered == {"queued": 0, "lost": 0}
    assert persisted["status"] == "interrupted"
    assert persisted["record"]["status"] == "interrupted"
    assert recovery_queue.empty()
    assert ad.list_async_delegations() == []


@pytest.mark.parametrize("invalid_profile", (None, 7, "other-profile"))
def test_execution_pipeline_tracker_profile_requires_exact_string_identity(
    tmp_path,
    monkeypatch,
    invalid_profile,
):
    from tests.tools.test_bestplan_review_recovery import (
        _seed_pre_review_execution,
    )

    seeded = _seed_pre_review_execution(tmp_path, monkeypatch)
    record = json.loads(seeded.tracker.read_text(encoding="utf-8"))[
        "records"
    ][seeded.request["delegation_id"]]["record"]
    if invalid_profile is None:
        record.pop("origin_profile")
    else:
        record["origin_profile"] = invalid_profile
    pipeline = seeded.review_store.get_execution_pipeline(seeded.plan_id)

    assert ad._execution_pipeline_matches_tracker_identity(
        pipeline,
        record,
        state_path=str(seeded.state_db),
    ) is False


@pytest.mark.parametrize("phase", ("intent", "running", "review_requeued"))
def test_startup_cancel_bridge_failure_queues_model_free_terminal_retry(
    tmp_path,
    monkeypatch,
    phase,
):
    from tests.tools.test_bestplan_review_recovery import (
        _seed_pre_review_execution,
    )

    seeded = _seed_pre_review_execution(tmp_path, monkeypatch)
    seeded.review_store.request_execution_pipeline_cancel(
        plan_id=seeded.plan_id,
        delegation_id=seeded.request["delegation_id"],
        job_id=seeded.request["job_id"],
    )
    payload = json.loads(seeded.tracker.read_text(encoding="utf-8"))
    entry = payload["records"][seeded.request["delegation_id"]]
    entry["status"] = phase
    entry["delivery_status"] = phase
    entry["record"]["status"] = phase
    entry["record"]["delivery_status"] = phase
    seeded.tracker.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(ad, "_owner_liveness", lambda _record: False)
    monkeypatch.setattr(
        ad,
        "_mark_bestplan_completed_unverified",
        lambda *_args, **_kwargs: False,
    )
    recovery_queue: queue.Queue = queue.Queue()

    recovered = ad.recover_async_delegations(
        seeded.tracker,
        target_queue=queue.Queue(),
        review_recovery_queue=recovery_queue,
    )

    retry = recovery_queue.get_nowait()
    assert recovered == {"queued": 0, "lost": 0, "review_requeued": 1}
    assert retry["_cancel_finalize_only"] is True
    assert recovery_queue.empty()
    assert seeded.review_store.get_execution_pipeline(
        seeded.plan_id
    ).state == "cancelled"


@pytest.mark.parametrize(
    "phase",
    (
        "intent",
        "scheduled",
        "running",
        "interrupting",
        "review_waiting",
        "review_requeued",
    ),
)
def test_unknown_owner_local_bestplan_is_visible_without_recovery_execution(
    tmp_path,
    monkeypatch,
    phase,
):
    from tests.tools.test_bestplan_review_recovery import (
        _seed_pre_review_execution,
    )

    seeded = _seed_pre_review_execution(tmp_path, monkeypatch)
    payload = json.loads(seeded.tracker.read_text(encoding="utf-8"))
    entry = payload["records"][seeded.request["delegation_id"]]
    entry["status"] = phase
    entry["delivery_status"] = phase
    entry["record"]["status"] = phase
    entry["record"]["delivery_status"] = phase
    seeded.tracker.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(ad, "_owner_liveness", lambda _record: None)
    recovery_queue: queue.Queue = queue.Queue()

    recovered = ad.recover_async_delegations(
        seeded.tracker,
        target_queue=queue.Queue(),
        review_recovery_queue=recovery_queue,
    )

    visible = {
        record["delegation_id"]: record
        for record in ad.list_async_delegations()
    }
    assert recovered == {"queued": 0, "lost": 0}
    assert recovery_queue.empty()
    assert visible[seeded.request["delegation_id"]]["status"] == phase
    assert visible[seeded.request["delegation_id"]]["owner_liveness"] == (
        "unknown"
    )
    pipeline = seeded.review_store.get_execution_pipeline(seeded.plan_id)
    assert pipeline.state == "pending"
    assert pipeline.cancel_requested is False


@pytest.mark.parametrize("phase", ("intent", "scheduled"))
def test_unknown_owner_preexecution_stop_waits_for_proven_extinction(
    tmp_path,
    monkeypatch,
    phase,
):
    from tests.tools.test_bestplan_review_recovery import (
        _seed_pre_review_execution,
    )

    seeded = _seed_pre_review_execution(tmp_path, monkeypatch)
    payload = json.loads(seeded.tracker.read_text(encoding="utf-8"))
    entry = payload["records"][seeded.request["delegation_id"]]
    entry["status"] = phase
    entry["delivery_status"] = phase
    entry["record"]["status"] = phase
    entry["record"]["delivery_status"] = phase
    seeded.tracker.write_text(json.dumps(payload), encoding="utf-8")
    owner_liveness = None
    monkeypatch.setattr(ad, "_owner_liveness", lambda _record: owner_liveness)
    recovery_queue: queue.Queue = queue.Queue()
    assert ad.recover_async_delegations(
        seeded.tracker,
        target_queue=queue.Queue(),
        review_recovery_queue=recovery_queue,
    ) == {"queued": 0, "lost": 0}

    assert ad.interrupt_all(reason="operator_stop") == 1

    pipeline = seeded.review_store.get_execution_pipeline(seeded.plan_id)
    persisted = json.loads(seeded.tracker.read_text(encoding="utf-8"))["records"][
        seeded.request["delegation_id"]
    ]
    assert pipeline.cancel_requested is True
    assert pipeline.state == "cancel_requested"
    assert persisted["status"] == "interrupting"
    assert persisted["record"]["status"] == "interrupting"
    assert recovery_queue.empty()
    with ad._records_lock:
        ad._records.clear()
    restarted_queue: queue.Queue = queue.Queue()
    assert ad.recover_async_delegations(
        seeded.tracker,
        target_queue=queue.Queue(),
        review_recovery_queue=restarted_queue,
    ) == {"queued": 0, "lost": 0}
    assert restarted_queue.empty()
    visible = {
        record["delegation_id"]: record
        for record in ad.list_async_delegations()
    }
    assert visible[seeded.request["delegation_id"]]["status"] == (
        "interrupting"
    )

    owner_liveness = False
    with ad._records_lock:
        ad._records.clear()
    assert ad.recover_async_delegations(
        seeded.tracker,
        target_queue=queue.Queue(),
        review_recovery_queue=restarted_queue,
    ) == {"queued": 0, "lost": 0}
    assert seeded.review_store.get_execution_pipeline(seeded.plan_id).state == (
        "cancelled"
    )
    assert restarted_queue.empty()


@pytest.mark.parametrize("phase", ("intent", "scheduled"))
def test_unknown_owner_preexecution_stop_finalizes_with_no_attempt_owner(
    tmp_path,
    monkeypatch,
    phase,
):
    from tests.tools.test_bestplan_review_recovery import (
        _seed_pre_review_execution,
    )

    seeded = _seed_pre_review_execution(tmp_path, monkeypatch)
    pipeline = seeded.review_store.get_execution_pipeline(seeded.plan_id)
    seeded.review_store.release_execution_attempt(
        seeded.plan_id,
        owner_pid=pipeline.attempt_owner_pid,
        owner_process_start_id=pipeline.attempt_owner_process_start_id,
    )
    payload = json.loads(seeded.tracker.read_text(encoding="utf-8"))
    entry = payload["records"][seeded.request["delegation_id"]]
    entry["status"] = phase
    entry["delivery_status"] = phase
    entry["record"]["status"] = phase
    entry["record"]["delivery_status"] = phase
    seeded.tracker.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(ad, "_owner_liveness", lambda _record: None)
    recovery_queue: queue.Queue = queue.Queue()
    assert ad.recover_async_delegations(
        seeded.tracker,
        target_queue=queue.Queue(),
        review_recovery_queue=recovery_queue,
    ) == {"queued": 0, "lost": 0}

    assert ad.interrupt_all(reason="operator_stop") == 1

    pipeline = seeded.review_store.get_execution_pipeline(seeded.plan_id)
    persisted = json.loads(seeded.tracker.read_text(encoding="utf-8"))["records"][
        seeded.request["delegation_id"]
    ]
    assert pipeline.state == "cancelled"
    assert pipeline.cancel_requested is True
    assert pipeline.active_attempt_ordinal is None
    assert pipeline.attempt_owner_pid is None
    assert persisted["status"] == "interrupted"
    assert persisted["record"]["status"] == "interrupted"
    assert recovery_queue.empty()


@pytest.mark.parametrize(
    "phase", ("running", "interrupting", "review_waiting", "review_requeued"),
)
def test_unknown_owner_active_stop_stays_interrupting_until_owner_is_dead(
    tmp_path,
    monkeypatch,
    phase,
):
    from tests.tools.test_bestplan_review_recovery import (
        _seed_pre_review_execution,
    )

    seeded = _seed_pre_review_execution(tmp_path, monkeypatch)
    payload = json.loads(seeded.tracker.read_text(encoding="utf-8"))
    entry = payload["records"][seeded.request["delegation_id"]]
    entry["status"] = phase
    entry["delivery_status"] = phase
    entry["record"]["status"] = phase
    entry["record"]["delivery_status"] = phase
    seeded.tracker.write_text(json.dumps(payload), encoding="utf-8")
    owner_liveness = None
    monkeypatch.setattr(ad, "_owner_liveness", lambda _record: owner_liveness)
    recovery_queue: queue.Queue = queue.Queue()
    assert ad.recover_async_delegations(
        seeded.tracker,
        target_queue=queue.Queue(),
        review_recovery_queue=recovery_queue,
    ) == {"queued": 0, "lost": 0}

    assert ad.interrupt_all(reason="operator_stop") == 1

    pipeline = seeded.review_store.get_execution_pipeline(seeded.plan_id)
    persisted = json.loads(seeded.tracker.read_text(encoding="utf-8"))["records"][
        seeded.request["delegation_id"]
    ]
    assert pipeline.cancel_requested is True
    assert pipeline.state == "cancel_requested"
    assert persisted["status"] == "interrupting"
    assert recovery_queue.empty()
    with ad._records_lock:
        ad._records.clear()
    assert ad.recover_async_delegations(
        seeded.tracker,
        target_queue=queue.Queue(),
        review_recovery_queue=recovery_queue,
    ) == {"queued": 0, "lost": 0}
    visible = {
        record["delegation_id"]: record
        for record in ad.list_async_delegations()
    }
    assert visible[seeded.request["delegation_id"]]["status"] == (
        "interrupting"
    )
    assert recovery_queue.empty()

    owner_liveness = False
    with ad._records_lock:
        ad._records.clear()
    assert ad.recover_async_delegations(
        seeded.tracker,
        target_queue=queue.Queue(),
        review_recovery_queue=recovery_queue,
    ) == {"queued": 0, "lost": 0}
    assert seeded.review_store.get_execution_pipeline(seeded.plan_id).state == (
        "cancelled"
    )
    assert recovery_queue.empty()


@pytest.mark.parametrize("phase", ("review_waiting", "review_requeued"))
def test_unknown_owner_review_stop_persists_job_cancel_without_worker(
    tmp_path,
    monkeypatch,
    phase,
):
    state_db_path, store, _claim = _claimed_review_job(tmp_path)
    tracker = (tmp_path / "unknown-review-owner.json").resolve()
    delegation_id = "deleg-unknown-review-owner"
    record = _register_local_bestplan(
        state_db_path=state_db_path,
        review_job_id="review-job-live",
        delegation_id=delegation_id,
        status=phase,
        interrupt_fn=None,
    )
    record["origin_tracker_path"] = str(tracker)
    assert ad._persist_record(record, delivery_status=phase)
    with ad._records_lock:
        ad._records.clear()
    monkeypatch.setattr(ad, "_owner_liveness", lambda _record: None)
    recovery_queue: queue.Queue = queue.Queue()
    assert ad.recover_async_delegations(
        tracker,
        target_queue=queue.Queue(),
        review_recovery_queue=recovery_queue,
    ) == {"queued": 0, "lost": 0}

    assert ad.interrupt_all(reason="operator_stop") == 1

    durable = store.get_job("review-job-live")
    persisted = json.loads(tracker.read_text(encoding="utf-8"))["records"][
        delegation_id
    ]
    assert durable.cancel_requested is True
    assert durable.state == "cancel_requested"
    assert persisted["status"] == "interrupting"
    assert recovery_queue.empty()


@pytest.mark.parametrize(
    "phase",
    (
        "intent",
        "scheduled",
        "running",
        "interrupting",
        "review_waiting",
        "review_requeued",
    ),
)
def test_unknown_owner_nonterminal_survives_all_retention_paths(
    tmp_path,
    monkeypatch,
    phase,
):
    tracker = (tmp_path / "unknown-retention.json").resolve()
    delegation_id = f"deleg-unknown-retention-{phase}"
    record = {
        "delegation_id": delegation_id,
        "status": phase,
        "delivery_status": phase,
        "owner_liveness": "unknown",
        "bestplan_local_execution": True,
        "bestplan_plan_id": "bp-retention",
        "bestplan_review_job_id": "job-retention",
        "bestplan_state_db_path": str((tmp_path / "state.db").resolve()),
        "origin_tracker_path": str(tracker),
        "dispatched_at": 1.0,
        "last_heartbeat_at": 1.0,
        "is_batch": True,
    }
    tracker.write_text(json.dumps({
        "version": 1,
        "records": {
            delegation_id: {
                "delegation_id": delegation_id,
                "record": record,
                "status": phase,
                "delivery_status": phase,
            },
        },
    }), encoding="utf-8")
    with ad._records_lock:
        ad._records[delegation_id] = dict(record)
        for index in range(55):
            ad._records[f"completed-{index}"] = {
                "delegation_id": f"completed-{index}",
                "status": "completed",
                "delivery_status": "delivered",
                "dispatched_at": 10.0 + index,
                "completed_at": 10.0 + index,
            }
    monkeypatch.setattr(ad, "_MAX_RETAINED_COMPLETED", 1)
    monkeypatch.setattr(
        ad,
        "_retention_policy_from_config",
        lambda: {
            "completed_seconds": 0.001,
            "failed_seconds": 0.001,
            "lost_seconds": 0.001,
            "max_bytes": 1,
        },
    )

    with ad._records_lock:
        ad._prune_completed_locked()
        ad._cleanup_locked(now=time.time() + 10_000)
    with ad._persist_lock:
        data = ad._read_persisted_unlocked(tracker)
        ad._cleanup_persisted_data_locked(data, now=time.time() + 10_000)
        ad._write_persisted_unlocked(data, tracker)
    ad._recovery_attempted = True

    for _ in range(2):
        visible = {
            item["delegation_id"]: item
            for item in ad.list_async_delegations()
        }
        assert visible[delegation_id]["status"] == phase
        assert visible[delegation_id]["owner_liveness"] == "unknown"
    persisted = json.loads(tracker.read_text(encoding="utf-8"))["records"]
    assert delegation_id in persisted
    selected: list[str] = []
    monkeypatch.setattr(
        ad,
        "_interrupt_records",
        lambda targets, **_kwargs: selected.extend(
            str(item["delegation_id"]) for item in targets
        ) or len(targets),
    )
    assert ad.interrupt_all(reason="operator_stop") == 1
    assert selected == [delegation_id]


def test_landing_prepared_crash_reconciles_exact_readback_without_git_replay(
    tmp_path, monkeypatch,
):
    from agent import bestplan_local_git
    from agent.bestplan_state import BestplanStore
    from tests.agent.test_bestplan_landing_gate import _git, _prepare_landing

    prepared = _prepare_landing(tmp_path)
    state_path = prepared.store.state_db_path
    assert prepared.review_store.get_job("review-job-local").state == (
        "landing_prepared"
    )
    replayed: list[str] = []
    monkeypatch.setattr(
        bestplan_local_git,
        "land_checked_integration",
        lambda **_kwargs: replayed.append("replayed")
        or pytest.fail("prepared crash recovery replayed the Git effect"),
    )
    prepared.store.close()
    reopened = BestplanStore(db_path=state_path, reconcile_push_state=False)

    # No observed effect means no blind landing and no review replay.
    assert reopened.reconcile_local_pushes(
        classify_local_main=lambda **_kwargs: "expected",
    ) == 0
    assert reopened.get_plan("bp-local")["local_push_state"] == "prepared"
    assert ReviewStore(state_path).get_job("review-job-local").state == (
        "landing_prepared"
    )

    # If exact read-back later proves the effect, reconcile both journals. Do
    # not invoke the landing function a second time.
    _git(
        prepared.repo,
        "merge",
        "--ff-only",
        prepared.integration.integration_oid,
    )
    assert reopened.reconcile_local_pushes(
        classify_local_main=lambda **_kwargs: "integration",
    ) == 1
    assert replayed == []
    assert reopened.get_plan("bp-local")["local_push_state"] == "awaiting"
    assert ReviewStore(state_path).get_job("review-job-local").state == "landed"
    assert _git(prepared.repo, "rev-parse", "HEAD") == (
        prepared.integration.integration_oid
    )


def test_recovered_pass_reclaim_handoff_lands_with_the_current_fence(tmp_path):
    import hashlib
    from types import SimpleNamespace

    from agent.review_engine import ReviewTarget
    from tests.agent.test_bestplan_landing_gate import (
        _frozen,
        _git,
        _integration_commit,
        _repo,
        _snapshot,
    )
    from tests.agent.test_bestplan_local_git import _configure_local_remote
    from tests.agent.test_bestplan_local_push_state import (
        _seed_local_plan,
        _seed_review_pass,
        _store,
    )
    from tools import delegate_tool

    repo = _repo(tmp_path)
    _configure_local_remote(repo, tmp_path)
    snapshot = _snapshot(repo)
    integration_oid, tree_oid = _integration_commit(
        repo,
        path="src/recovered.py",
        content=b"RECOVERED = True\n",
    )
    integration, checks, commands = _frozen(
        snapshot, integration_oid, tree_oid,
    )
    plan_store = _store(tmp_path)
    _seed_local_plan(plan_store, snapshot, integration_oid)
    row = plan_store.get_plan("bp-local")
    target = ReviewTarget.bestplan_integration(
        plan_id="bp-local",
        generation=0,
        base_oid=snapshot.head_oid,
        local_target_oid=snapshot.head_oid,
        integration_oid=integration_oid,
        integration_tree_oid=tree_oid,
        integration_ref="refs/hermes-bestplan-integrations/bp-local/0",
        integration_receipt_digest=integration.receipt_digest,
        check_receipt_digest=checks.receipt_digest,
        approval_digest=row["approval_digest"],
        contract_digest=row["promotion_contract_digest"],
        diff_sha256=hashlib.sha256(b"recovered diff").hexdigest(),
        acceptance_digest=hashlib.sha256(b"recovered acceptance").hexdigest(),
        policy_digest=hashlib.sha256(b"two reviewers").hexdigest(),
    )
    review_store, first_claim, receipt_digest = _seed_review_pass(
        plan_store, target,
    )
    reclaimed = review_store.claim_job(
        job_id="review-job-local",
        owner_id="review-worker-after-restart",
        now_ns=int(first_claim.lease_expires_at_ns) + 1,
        lease_duration_ns=1_000_000,
        expected_fencing_token=first_claim.fencing_token,
    )
    assert reclaimed.fencing_token > first_claim.fencing_token
    reviewed = delegate_tool._LocalBestplanReviewResult(
        integration=integration,
        checks=checks,
        target=target,
        receipt=SimpleNamespace(receipt_digest=receipt_digest, passed=True),
        job_id="review-job-local",
        owner_id=reclaimed.owner_id,
        fencing_token=reclaimed.fencing_token,
    )

    completion = delegate_tool._land_reviewed_local_bestplan(
        plan_id="bp-local",
        snapshot=snapshot,
        runtime=SimpleNamespace(
            check_plan=SimpleNamespace(commands=commands),
        ),
        state_db_path=Path(plan_store.state_db_path),
        session_id="session-1",
        profile="coder",
        reviewed=reviewed,
        projected_results=({"status": "frozen", "summary": "recovered"},),
        deadline=time.monotonic() + 20,
        cancel_event=None,
    )

    assert completion["local_main_oid"] == integration_oid
    assert ReviewStore(plan_store.state_db_path).get_job(
        "review-job-local"
    ).state == "landed"
    assert _git(repo, "rev-parse", "HEAD") == integration_oid
    with pytest.raises(ReviewLeaseConflict):
        ReviewStore(plan_store.state_db_path).request_cancel(
            job_id="review-job-local",
            owner_id=first_claim.owner_id,
            fencing_token=first_claim.fencing_token,
            operation_id="stale-owner-after-recovered-landing",
            signal_children=lambda: None,
        )


def test_live_review_no_change_and_failed_recheck_retry_with_fresh_ordinals_and_deadlines(
    tmp_path, monkeypatch,
):
    """The live loop keeps repairing; neither retry reuses an old attempt."""

    import hashlib
    import inspect
    from dataclasses import replace
    from types import SimpleNamespace

    from agent import bestplan_candidates, bestplan_checks, bestplan_promotion
    from agent import bestplan_review, review_engine
    from agent.review_engine import (
        EvidenceContext,
        ReviewArtifact,
        ReviewTarget,
        build_review_packet,
    )
    from tests.agent.test_bestplan_local_flow import (
        _review_generation_receipt,
        _review_loop_inputs,
    )
    from tools import delegate_tool

    inputs = _review_loop_inputs(tmp_path)
    generation_by_oid = {
        inputs.integration(generation).integration_oid: generation
        for generation in range(4)
    }
    repair_attempts: list[dict[str, object]] = []
    review_generations: list[int] = []
    check_generations: list[int] = []
    operation_deadlines: list[tuple[str, int, float]] = []
    no_change_retries = 12

    def build_bundle(**kwargs):
        generation = kwargs["generation"]
        integration = kwargs["integration"]
        checks = kwargs["checks"]
        operation_deadlines.append(("review", generation, kwargs["deadline"]))
        assert generation_by_oid[integration.integration_oid] == generation
        assert checks.integration_oid == integration.integration_oid
        target = ReviewTarget.bestplan_integration(
            plan_id="bp-local",
            generation=generation,
            base_oid=inputs.snapshot.head_oid,
            local_target_oid=integration.target_oid,
            integration_oid=integration.integration_oid,
            integration_tree_oid=integration.tree_oid,
            integration_ref=integration.ref_name,
            integration_receipt_digest=integration.receipt_digest,
            check_receipt_digest=checks.receipt_digest,
            approval_digest="5" * 64,
            contract_digest="6" * 64,
            diff_sha256=hashlib.sha256(b"").hexdigest(),
            acceptance_digest="7" * 64,
            policy_digest=kwargs["policy_digest"],
        )
        artifact = ReviewArtifact.build(
            target=target,
            diff_bytes=b"",
            task="Repair until the exact implementation passes",
            acceptance=("All approved checks and both reviewers pass",),
            rules=("A failed host check is blocking repair evidence",),
            issue_locator_catalog={},
            dispositions=kwargs["dispositions"],
        )
        evidence = EvidenceContext(
            read_frozen_file=lambda _path: b"fixed\n",
            diff_membership=lambda _path, _start, _end: True,
            approved_lease_paths=("slice-a/", "slice-b/"),
        )
        return bestplan_review.BestplanReviewBundle(
            target=target,
            artifact=artifact,
            evidence=evidence,
            packet=build_review_packet(target, artifact=artifact),
            diff_bytes=b"",
        )

    def run_review(target, _runtimes, **kwargs):
        review_generations.append(target.generation)
        receipt = _review_generation_receipt(
            target, blocked=target.generation == 0,
        )
        callback = kwargs.get("receipt_callback")
        if callback is not None:
            for reviewer_receipt in receipt.reviewer_receipts:
                callback(reviewer_receipt, "{}")
        return receipt

    def repair_candidate(**kwargs):
        spec = kwargs["spec"]
        ordinal = len(repair_attempts)
        repair_attempts.append({
            "plan_id": spec.plan_id,
            "candidate_id": spec.candidate_id,
            "slice_id": spec.slice_id,
            "attempt_id": kwargs["attempt_id"],
        })
        frozen = inputs.frozen(
            spec,
            kwargs["attempt_id"],
            max(1, ordinal),
        )
        if ordinal < no_change_retries:
            return replace(frozen, changed_paths=())
        return frozen

    def freeze_repair(**kwargs):
        generation = kwargs["generation"]
        operation_deadlines.append(("freeze", generation, kwargs["deadline"]))
        return inputs.integration(generation)

    def run_checks(**kwargs):
        generation = generation_by_oid[kwargs["integration"].integration_oid]
        check_generations.append(generation)
        operation_deadlines.append(("checks", generation, kwargs["deadline"]))
        if len(check_generations) == 1:
            raise bestplan_checks.CheckExecutionError(
                "enrollment-bound check returned nonzero"
            )
        return inputs.checks(generation)

    monkeypatch.setattr(
        bestplan_review, "build_bestplan_review_bundle", build_bundle,
    )
    monkeypatch.setattr(review_engine, "run_review_generation", run_review)
    monkeypatch.setattr(
        bestplan_candidates,
        "run_and_freeze_repair_candidate",
        repair_candidate,
    )
    monkeypatch.setattr(
        bestplan_promotion, "freeze_repair_integration", freeze_repair,
    )
    monkeypatch.setattr(bestplan_checks, "run_integration_checks", run_checks)

    reviewer_authorities = (
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
    state_db_path = tmp_path / "state.db"
    assert not {
        "max_rounds",
        "max_review_rounds",
        "max_generations",
        "review_round_limit",
    }.intersection(
        inspect.signature(
            delegate_tool._run_local_bestplan_review_loop
        ).parameters
    )
    result = delegate_tool._run_local_bestplan_review_loop(
        plan_id="bp-local",
        raw_request="Repair until the exact implementation passes",
        plan=inputs.plan,
        snapshot=inputs.snapshot,
        contract={"schema": "test.local-review", "commands": []},
        approval_digest="5" * 64,
        contract_digest="6" * 64,
        completed=inputs.completed,
        candidate_authorities=(object(), object()),
        review_authority_bindings=reviewer_authorities,
        integration=inputs.integration(0),
        checks=inputs.checks(0),
        runtime=inputs.runtime,
        state_db_path=state_db_path,
        session_id="session-local",
        profile="default",
        deadline=time.monotonic() - 60,
        cancel_event=None,
    )

    assert result.receipt.passed is True
    assert result.target.generation == 2
    assert review_generations == [0, 2]
    assert check_generations == [1, 2]
    assert len(repair_attempts) == no_change_retries + 2
    for field in ("plan_id", "candidate_id", "slice_id", "attempt_id"):
        assert len({attempt[field] for attempt in repair_attempts}) == (
            no_change_retries + 2
        )

    stored_events = ReviewStore(state_db_path).list_events(result.job_id)
    no_change = [event for event in stored_events if event.kind == "repair_no_change"]
    assert len(no_change) == no_change_retries
    assert [
        json.loads(event.payload_json)["repair_attempt"]
        for event in no_change
    ] == list(range(no_change_retries))
    assert any(event.kind == "host_check_failed" for event in stored_events)

    # Each repair/check/review operation gets a new bounded deadline. The
    # expired dispatch deadline is not a total loop deadline.
    assert len({deadline for _kind, _generation, deadline in operation_deadlines}) > 1
    assert operation_deadlines[-1][2] > operation_deadlines[0][2]


@pytest.mark.parametrize(
    "status",
    ("error", "failed", "interrupted", "lost"),
)
def test_batch_failure_notification_never_says_complete(status):
    rendered = _format_async_delegation(
        {
            "delegation_id": f"deleg-{status}",
            "is_batch": True,
            "status": status,
            "goals": ["implement exact change"],
            "results": [],
            "error": "review did not pass",
        }
    )

    assert "BATCH COMPLETE" not in rendered
    assert "BATCH ERROR" in rendered
    assert "did not complete successfully" in rendered


def test_batch_error_payload_never_says_complete_even_with_completed_status():
    rendered = _format_async_delegation(
        {
            "delegation_id": "deleg-contradictory-status",
            "is_batch": True,
            "status": "completed",
            "goals": ["implement exact change"],
            "results": [],
            "error": "review did not pass",
        }
    )

    assert "BATCH COMPLETE" not in rendered
    assert "BATCH ERROR" in rendered
    assert "review did not pass" in rendered


def test_batch_failed_child_never_says_complete_when_siblings_finished():
    rendered = _format_async_delegation(
        {
            "delegation_id": "deleg-mixed-results",
            "is_batch": True,
            "status": "completed",
            "goals": ["first", "second"],
            "results": [
                {"task_index": 0, "status": "completed", "summary": "ok"},
                {
                    "task_index": 1,
                    "status": "error",
                    "error": "repair failed",
                },
            ],
        }
    )

    assert "BATCH COMPLETE" not in rendered
    assert "BATCH ERROR" in rendered
    assert "status=error" in rendered


@pytest.mark.parametrize(
    "status",
    ("review_waiting", "review_requeued", "blocked_requires_authority"),
)
def test_bestplan_waiting_notification_never_says_complete(status):
    rendered = _format_async_delegation(
        {
            "delegation_id": f"deleg-{status}",
            "is_batch": True,
            "status": status,
            "bestplan_plan_id": "bp-local",
            "bestplan_local_execution": True,
            "goals": ["implement exact change"],
            "results": [],
        }
    )

    assert "BATCH COMPLETE" not in rendered
    assert "BESTPLAN REVIEW WAITING" in rendered
    assert "has not finished" in rendered
