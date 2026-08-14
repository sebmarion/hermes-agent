"""Crash-recovery contracts for the automatic BestPlan review gate.

These tests describe durable boundaries.  A restarted process must rebuild the
host adapter from stored, non-secret identity data and continue from the first
unfinished side effect.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _logical_review_store_clock(monkeypatch):
    """Keep this store-unit suite on its explicit synthetic nanosecond clock."""

    monkeypatch.setattr(
        _review().ReviewStore,
        "_lease_now_ns",
        lambda _self: 0,
    )


def _review():
    from agent import review_engine

    return review_engine


def _target(review, *, generation: int = 0):
    digit = format(generation + 3, "x")
    return review.ReviewTarget.bestplan_integration(
        plan_id="recoverable-plan",
        generation=generation,
        base_oid="1" * 40,
        local_target_oid="2" * 40,
        integration_oid=digit * 40,
        integration_tree_oid=format(generation + 7, "x") * 40,
        integration_ref=(
            "refs/hermes-bestplan-integrations/recoverable-plan/"
            f"{generation}"
        ),
        integration_receipt_digest=format(generation + 8, "x") * 64,
        check_receipt_digest=format(generation + 9, "x") * 64,
        approval_digest="a" * 64,
        contract_digest="b" * 64,
        diff_sha256=format(generation + 12, "x") * 64,
        acceptance_digest="d" * 64,
        policy_digest="e" * 64,
    )


def _adapter_state(workspace: Path) -> dict[str, object]:
    return {
        "schema": "hermes.bestplan.local-review-adapter.v1",
        "approval_digest": "a" * 64,
        "contract_digest": "b" * 64,
        "manifest_digest": "c" * 64,
        "source_snapshot_receipt_digest": "d" * 64,
        "approved_slices": [
            {
                "slice_id": "slice-a",
                "allowed_paths": ["agent/"],
                "expected_artifacts": ["agent/result.py"],
            }
        ],
        "snapshot_manifest": str(workspace / ".hermes-review" / "snapshot.json"),
    }


def _runtime_routes(secret: str = "") -> list[dict[str, object]]:
    routes: list[dict[str, object]] = [
        {
            "route": "candidate-0",
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
            "runtime_fingerprint": "1" * 64,
        },
        {
            "route": "smart_reviewer",
            "provider": "anthropic",
            "model": "claude-opus-5",
            "runtime_fingerprint": "2" * 64,
        },
        {
            "route": "code_worker",
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
            "runtime_fingerprint": "3" * 64,
        },
    ]
    if secret:
        for route in routes:
            route["api_key"] = secret
            route["headers"] = {"authorization": f"Bearer {secret}"}
    return routes


def _create_recoverable_job(store, target, workspace: Path, *, secret: str = ""):
    return store.create_job(
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
        adapter_state=_adapter_state(workspace.resolve()),
        runtime_routes=_runtime_routes(secret),
    )


def _claim_and_start(store, target, workspace: Path, *, secret: str = ""):
    _create_recoverable_job(store, target, workspace, secret=secret)
    claim = store.claim_job(
        job_id="review-job-recovery",
        owner_id="worker-before-crash",
        now_ns=1_000,
        lease_duration_ns=100,
        expected_fencing_token=0,
    )
    store.begin_generation(
        job_id="review-job-recovery",
        generation=target.generation,
        target=target,
        owner_id=claim.owner_id,
        fencing_token=claim.fencing_token,
        operation_id=f"generation-{target.generation}",
    )
    return claim


def _reclaim(store):
    old = store.get_job("review-job-recovery")
    return store.claim_job(
        job_id="review-job-recovery",
        owner_id="worker-after-restart",
        now_ns=1_101,
        lease_duration_ns=1_000,
        expected_fencing_token=old.fencing_token,
    )


def _verdict_payload(target, slot: str, *, blocker: bool) -> str:
    findings = []
    if blocker:
        findings.append(
            {
                "severity": "high",
                "fingerprint": "f" * 64,
                "locator": {
                    "kind": "changed_lines",
                    "path": "agent/result.py",
                    "start_line": 1,
                    "end_line": 1,
                    "quoted_evidence": "broken\n",
                    "cited_bytes_sha256": hashlib.sha256(b"broken\n").hexdigest(),
                },
                "title": "The generated result is still broken",
                "trigger": "Import agent.result",
                "observed_failure": "The module raises during import",
                "blast_radius": "The approved BestPlan result",
                "reproduction": {
                    "kind": "command",
                    "argv": ["python", "-c", "import agent.result"],
                },
            }
        )
    return json.dumps(
        {
            "schema": "hermes.bestplan.stored-reviewer-receipt.v1",
            "slot": slot,
            "provider": "anthropic" if slot == "smart_reviewer" else "openai-codex",
            "model": "claude-opus-5" if slot == "smart_reviewer" else "gpt-5.6-sol",
            "model_family": "claude" if slot == "smart_reviewer" else "gpt",
            "runtime_fingerprint": "2" * 64 if slot == "smart_reviewer" else "3" * 64,
            "target_digest": target.target_digest,
            "integration_oid": target.integration_oid,
            "findings": findings,
        },
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _record_slot(store, target, claim, slot: str, *, blocker: bool = False):
    receipt_json = _verdict_payload(target, slot, blocker=blocker)
    return store.record_reviewer_receipt(
        job_id="review-job-recovery",
        generation=target.generation,
        slot=slot,
        target_digest=target.target_digest,
        integration_oid=target.integration_oid,
        output_digest=hashlib.sha256(receipt_json.encode("utf-8")).hexdigest(),
        verdict_digest=hashlib.sha256(
            b"host-verdict\0" + receipt_json.encode("utf-8")
        ).hexdigest(),
        passed=not blocker,
        receipt_json=receipt_json,
        owner_id=claim.owner_id,
        fencing_token=claim.fencing_token,
        operation_id=f"reviewer-{target.generation}-{slot}",
    )


def _record_blocked(store, target, claim):
    _record_slot(store, target, claim, "smart_reviewer", blocker=True)
    _record_slot(store, target, claim, "code_worker")
    blockers_json = json.dumps(
        json.loads(_verdict_payload(target, "smart_reviewer", blocker=True))[
            "findings"
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    review_receipt_digest = hashlib.sha256(
        b"blocked-review-receipt\0" + blockers_json.encode("utf-8")
    ).hexdigest()
    store.record_generation_blocked(
        job_id="review-job-recovery",
        generation=target.generation,
        target_digest=target.target_digest,
        integration_oid=target.integration_oid,
        check_receipt_digest=target.check_receipt_digest,
        review_receipt_digest=review_receipt_digest,
        blocking_findings_json=blockers_json,
        owner_id=claim.owner_id,
        fencing_token=claim.fencing_token,
        operation_id=f"blocked-{target.generation}",
    )
    return blockers_json, review_receipt_digest


def _record_repair_frozen(store, prior_target, claim):
    candidate_receipts = [
        {
            "slice_id": "slice-a",
            "candidate_ref": "refs/hermes-bestplan-candidates/recoverable-plan/1/a",
            "candidate_oid": "6" * 40,
            "candidate_receipt_digest": "7" * 64,
            "changed_paths": ["agent/result.py"],
        }
    ]
    candidate_receipts_json = json.dumps(
        candidate_receipts, sort_keys=True, separators=(",", ":")
    )
    store.record_repair_frozen(
        job_id="review-job-recovery",
        prior_generation=prior_target.generation,
        generation=prior_target.generation + 1,
        prior_target_digest=prior_target.target_digest,
        integration_oid="4" * 40,
        integration_tree_oid="8" * 40,
        integration_ref="refs/hermes-bestplan-integrations/recoverable-plan/1",
        integration_receipt_digest="9" * 64,
        candidate_receipts_json=candidate_receipts_json,
        owner_id=claim.owner_id,
        fencing_token=claim.fencing_token,
        operation_id="repair-frozen-1",
    )
    return candidate_receipts_json


def _repair_candidate_receipt(
    *, manifest_slice_id: str = "slice-a", repair_attempt: int = 0,
) -> tuple[str, str]:
    from tools import delegate_tool

    attempt_plan_id = delegate_tool._bestplan_safe_identifier(
        "repair-plan", "recoverable-plan", 1, repair_attempt,
    )
    candidate_id = delegate_tool._bestplan_safe_identifier(
        "candidate", attempt_plan_id, 0, manifest_slice_id,
    )
    slice_id = delegate_tool._bestplan_safe_identifier(
        "slice", attempt_plan_id, 0, manifest_slice_id,
    )
    attempt_id = delegate_tool._bestplan_safe_identifier(
        "attempt", attempt_plan_id, 0, manifest_slice_id, repair_attempt,
    )
    worker_receipt = {"schema": "test.frozen-candidate.v1", "status": "ok"}
    worker_receipt_json = json.dumps(
        worker_receipt,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    changed_paths = ["agent/result.py"]
    receipt = {
        "schema": "hermes.bestplan.host-candidate-receipt.v1",
        "manifest_slice_id": manifest_slice_id,
        "candidate_id": candidate_id,
        "slice_id": slice_id,
        "attempt_id": attempt_id,
        "candidate_ref": (
            f"refs/hermes-bestplan/{attempt_plan_id}/{slice_id}/{attempt_id}"
        ),
        "commit_oid": "6" * 40,
        "tree_oid": "7" * 40,
        "policy_digest": "8" * 64,
        "candidate_expires_at": 2_000_000_000,
        "promotion_contract_digest": "b" * 64,
        "changed_paths": {
            "count": 1,
            "sha256": delegate_tool._bestplan_changed_paths_digest(
                tuple(path.encode("utf-8") for path in changed_paths)
            ),
        },
        "controller": {
            "id": "controller-id",
            "repository_id": "repository-id",
            "release_oid": "9" * 40,
            "artifact_sha256": "a" * 64,
        },
        "admitted": {"requests": 1, "input_tokens": 2, "output_tokens": 3},
        "worker_receipt": worker_receipt,
        "worker_receipt_sha256": hashlib.sha256(
            worker_receipt_json.encode("utf-8")
        ).hexdigest(),
    }
    return (
        json.dumps(receipt, sort_keys=True, separators=(",", ":")),
        json.dumps(changed_paths, sort_keys=True, separators=(",", ":")),
    )


def test_review_store_persists_reconstructable_adapter_identity_without_credentials(
    tmp_path,
):
    review = _review()
    database = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = "provider-secret-must-not-reach-disk"
    target = _target(review)

    _create_recoverable_job(
        review.ReviewStore(database), target, workspace, secret=secret
    )

    reopened = review.ReviewStore(database)
    job = reopened.get_job("review-job-recovery")
    assert job.adapter_version == "local-bestplan.v1"
    assert job.owner_session_id == "session-recovery"
    assert job.owner_profile == "profile-recovery"
    assert job.workspace == str(workspace.resolve())
    assert json.loads(job.adapter_state_json) == _adapter_state(workspace.resolve())
    stored_routes = json.loads(job.runtime_routes_json)
    assert [(item["route"], item["runtime_fingerprint"]) for item in stored_routes] == [
        ("candidate-0", "1" * 64),
        ("smart_reviewer", "2" * 64),
        ("code_worker", "3" * 64),
    ]
    assert all("api_key" not in item and "headers" not in item for item in stored_routes)
    assert secret.encode("utf-8") not in database.read_bytes()


def test_review_store_rejects_in_memory_callbacks_from_recovery_metadata(tmp_path):
    review = _review()
    database = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = _target(review)

    with pytest.raises(review.ReviewValidationError, match="callback"):
        review.ReviewStore(database).create_job(
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
            adapter_state={"repair_callback": lambda: None},
            runtime_routes=_runtime_routes(),
        )


def test_restart_after_first_reviewer_receipt_runs_only_the_missing_slot(tmp_path):
    review = _review()
    database = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = _target(review)
    before = review.ReviewStore(database)
    first_claim = _claim_and_start(before, target, workspace)
    stored = _record_slot(before, target, first_claim, "smart_reviewer")

    restarted = review.ReviewStore(database)
    claim = _reclaim(restarted)
    resume = restarted.resume_job(
        job_id="review-job-recovery",
        owner_id=claim.owner_id,
        fencing_token=claim.fencing_token,
    )

    assert resume.next_action == "review_missing_slots"
    assert resume.missing_reviewer_slots == ("code_worker",)
    assert tuple(item.slot for item in resume.adopted_reviewer_receipts) == (
        "smart_reviewer",
    )
    assert resume.adopted_reviewer_receipts[0].receipt_json == stored.receipt_json
    assert json.loads(stored.receipt_json)["runtime_fingerprint"] == "2" * 64


def test_restart_after_blocker_commit_starts_repair_without_reviewer_replay(tmp_path):
    review = _review()
    database = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = _target(review)
    before = review.ReviewStore(database)
    first_claim = _claim_and_start(before, target, workspace)
    blockers_json, review_receipt_digest = _record_blocked(
        before, target, first_claim
    )

    restarted = review.ReviewStore(database)
    claim = _reclaim(restarted)
    resume = restarted.resume_job(
        job_id="review-job-recovery",
        owner_id=claim.owner_id,
        fencing_token=claim.fencing_token,
    )

    assert resume.next_action == "repair"
    assert resume.missing_reviewer_slots == ()
    assert resume.blocking_findings_json == blockers_json
    assert resume.review_receipt_digest == review_receipt_digest
    assert {item.slot for item in resume.adopted_reviewer_receipts} == {
        "smart_reviewer",
        "code_worker",
    }


def test_restart_adopts_each_durable_repair_candidate_without_provider_replay(
    tmp_path,
):
    review = _review()
    database = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = _target(review)
    before = review.ReviewStore(database)
    claim = _claim_and_start(before, target, workspace)
    _record_blocked(before, target, claim)
    candidate_receipt_json, changed_paths_json = _repair_candidate_receipt()
    candidate_receipt = json.loads(candidate_receipt_json)
    attempt_plan_id = candidate_receipt["candidate_ref"].split("/")[2]
    started = before.append_event(
        job_id="review-job-recovery",
        generation=0,
        owner_id=claim.owner_id,
        fencing_token=claim.fencing_token,
        operation_id="repair-attempt-started-slice-a-0",
        kind="repair_attempt_started",
        target_digest=target.target_digest,
        payload={
            "manifest_slice_id": "slice-a",
            "repair_attempt": 0,
        },
    )
    stored = before.record_repair_candidate_frozen(
        job_id="review-job-recovery",
        prior_generation=0,
        prior_target_digest=target.target_digest,
        base_integration_oid=target.integration_oid,
        manifest_slice_id="slice-a",
        repair_attempt=0,
        attempt_plan_id=attempt_plan_id,
        candidate_receipt_json=candidate_receipt_json,
        changed_paths_json=changed_paths_json,
        owner_id=claim.owner_id,
        fencing_token=claim.fencing_token,
        operation_id="repair-candidate-frozen-slice-a-0",
    )

    restarted = review.ReviewStore(database)
    durable = restarted.list_repair_candidates(
        "review-job-recovery", prior_generation=0,
    )

    assert durable == (stored,)
    assert durable[0].manifest_slice_id == "slice-a"
    assert durable[0].repair_attempt == 0
    assert durable[0].base_integration_oid == target.integration_oid
    assert json.loads(durable[0].candidate_receipt_json)["commit_oid"] == "6" * 40
    assert json.loads(durable[0].changed_paths_json) == ["agent/result.py"]
    assert started.event_seq < restarted.list_events(
        "review-job-recovery"
    )[-1].event_seq

    conflicting_receipt_json, conflicting_paths_json = (
        _repair_candidate_receipt(repair_attempt=1)
    )
    conflicting_receipt = json.loads(conflicting_receipt_json)
    with pytest.raises(review.ReviewStoreConflict):
        restarted.record_repair_candidate_frozen(
            job_id="review-job-recovery",
            prior_generation=0,
            prior_target_digest=target.target_digest,
            base_integration_oid=target.integration_oid,
            manifest_slice_id="slice-a",
            repair_attempt=1,
            attempt_plan_id=conflicting_receipt["candidate_ref"].split("/")[2],
            candidate_receipt_json=conflicting_receipt_json,
            changed_paths_json=conflicting_paths_json,
            owner_id=claim.owner_id,
            fencing_token=claim.fencing_token,
            operation_id="conflicting-repair-candidate",
        )


def test_restart_after_repair_freeze_adopts_it_and_runs_only_fresh_checks(tmp_path):
    review = _review()
    database = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = _target(review)
    before = review.ReviewStore(database)
    first_claim = _claim_and_start(before, target, workspace)
    _record_blocked(before, target, first_claim)
    candidate_receipts_json = _record_repair_frozen(
        before, target, first_claim
    )

    restarted = review.ReviewStore(database)
    claim = _reclaim(restarted)
    resume = restarted.resume_job(
        job_id="review-job-recovery",
        owner_id=claim.owner_id,
        fencing_token=claim.fencing_token,
    )

    assert resume.next_action == "checks"
    assert resume.generation == 1
    assert resume.repair_checkpoint.integration_oid == "4" * 40
    assert resume.repair_checkpoint.integration_ref.endswith(
        "/recoverable-plan/1"
    )
    assert (
        resume.repair_checkpoint.candidate_receipts_json
        == candidate_receipts_json
    )


def test_restart_after_fresh_checks_reviews_new_target_without_repeating_checks(
    tmp_path,
):
    review = _review()
    database = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first_target = _target(review)
    next_target = _target(review, generation=1)
    before = review.ReviewStore(database)
    first_claim = _claim_and_start(before, first_target, workspace)
    _record_blocked(before, first_target, first_claim)
    _record_repair_frozen(before, first_target, first_claim)
    check_receipt_json = json.dumps(
        {
            "schema": "hermes.bestplan.check-receipt.v1",
            "integration_oid": next_target.integration_oid,
            "receipt_digest": next_target.check_receipt_digest,
            "status": "passed",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    before.record_checks_passed(
        job_id="review-job-recovery",
        generation=1,
        target=next_target,
        check_receipt_json=check_receipt_json,
        owner_id=first_claim.owner_id,
        fencing_token=first_claim.fencing_token,
        operation_id="checks-passed-1",
    )

    restarted = review.ReviewStore(database)
    claim = _reclaim(restarted)
    resume = restarted.resume_job(
        job_id="review-job-recovery",
        owner_id=claim.owner_id,
        fencing_token=claim.fencing_token,
    )

    assert resume.next_action == "review_missing_slots"
    assert resume.generation == 1
    assert resume.target_digest == next_target.target_digest
    assert resume.check_receipt_json == check_receipt_json
    assert resume.missing_reviewer_slots == ("smart_reviewer", "code_worker")


def test_initial_check_pass_checkpoint_keeps_adapter_identity_immutable(tmp_path):
    review = _review()
    database = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = _target(review)
    adapter_state = {
        **_adapter_state(workspace.resolve()),
        "initial_check_pending": {
            "integration": {"schema": "exact-integration-receipt"},
        },
    }
    store = review.ReviewStore(database)
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
        workspace=str(workspace.resolve()),
        adapter_state=adapter_state,
        runtime_routes=_runtime_routes(),
    )
    claim = store.claim_job(
        job_id="review-job-recovery",
        owner_id="initial-check-worker",
        now_ns=1_000,
        lease_duration_ns=10_000,
        expected_fencing_token=0,
    )
    check_receipt_json = json.dumps(
        {
            "check_set": {"schema": "exact-check-set-receipt"},
            "dispositions": [],
            "schema": "hermes.bestplan.review-checkpoint.v1",
            "status": "passed",
        },
        sort_keys=True,
        separators=(",", ":"),
    )

    store.resolve_initial_check_pending(
        job_id="review-job-recovery",
        target=target,
        check_receipt_json=check_receipt_json,
        owner_id=claim.owner_id,
        fencing_token=claim.fencing_token,
        operation_id="initial-checks-passed",
    )

    durable = store.get_job("review-job-recovery")
    resume = store.resume_job(
        job_id=durable.job_id,
        owner_id=claim.owner_id,
        fencing_token=claim.fencing_token,
    )
    assert json.loads(durable.adapter_state_json) == adapter_state
    assert resume.next_action == "review_missing_slots"
    assert resume.check_receipt_json == check_receipt_json


def test_restart_after_pass_persistence_hands_off_without_review_or_git_replay(
    tmp_path,
):
    review = _review()
    database = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = _target(review)
    before = review.ReviewStore(database)
    first_claim = _claim_and_start(before, target, workspace)
    _record_slot(before, target, first_claim, "smart_reviewer")
    _record_slot(before, target, first_claim, "code_worker")
    pass_digest = "f" * 64
    stored_pass = before.record_generation_pass(
        job_id="review-job-recovery",
        generation=0,
        target_digest=target.target_digest,
        integration_oid=target.integration_oid,
        check_receipt_digest=target.check_receipt_digest,
        review_receipt_digest=pass_digest,
        owner_id=first_claim.owner_id,
        fencing_token=first_claim.fencing_token,
        operation_id="review-pass-0",
    )

    restarted = review.ReviewStore(database)
    claim = _reclaim(restarted)
    resume = restarted.resume_job(
        job_id="review-job-recovery",
        owner_id=claim.owner_id,
        fencing_token=claim.fencing_token,
    )

    assert resume.next_action == "handoff_pass"
    assert resume.missing_reviewer_slots == ()
    assert resume.review_pass == stored_pass
    assert resume.review_pass.review_receipt_digest == pass_digest


def test_wait_for_host_is_durable_and_resumes_without_replaying_side_effect(
    tmp_path,
):
    review = _review()
    database = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = _target(review)
    before = review.ReviewStore(database)
    claim = _claim_and_start(before, target, workspace)

    reason_code = "blocked_requires_authority"
    waiting = before.wait_for_host(
        job_id="review-job-recovery",
        generation=0,
        target_digest=target.target_digest,
        owner_id=claim.owner_id,
        fencing_token=claim.fencing_token,
        operation_id=f"wait-{reason_code}",
        reason_code=reason_code,
        payload={"reason_code": reason_code},
    )

    assert waiting.state == "waiting"
    restarted = review.ReviewStore(database)
    reclaimed = _reclaim(restarted)
    resume = restarted.resume_job(
        job_id="review-job-recovery",
        owner_id=reclaimed.owner_id,
        fencing_token=reclaimed.fencing_token,
    )
    assert resume.next_action == "wait_for_host"
    assert restarted.list_events("review-job-recovery")[-1].kind == reason_code


@pytest.mark.parametrize("reason_code", ("repair_no_change", "checks_failed"))
def test_wait_for_host_rejects_automatic_repair_reasons(tmp_path, reason_code):
    review = _review()
    database = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = _target(review)
    store = review.ReviewStore(database)
    claim = _claim_and_start(store, target, workspace)

    with pytest.raises(review.ReviewValidationError):
        store.wait_for_host(
            job_id="review-job-recovery",
            generation=0,
            target_digest=target.target_digest,
            owner_id=claim.owner_id,
            fencing_token=claim.fencing_token,
            operation_id=f"wait-{reason_code}",
            reason_code=reason_code,
            payload={"reason_code": reason_code},
        )
