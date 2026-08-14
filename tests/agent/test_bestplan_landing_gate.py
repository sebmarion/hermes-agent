from __future__ import annotations

import gc
import hashlib
import os
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import bestplan_local_git
from agent.bestplan_local_git import LocalMainLandingReceipt, LocalMainProofStale
from agent.bestplan_state import BestplanStore
from agent.review_engine import ReviewLeaseConflict, ReviewStore, ReviewTarget
from gateway.status import get_process_start_time
from tests.agent.test_bestplan_local_git import (
    _frozen,
    _git,
    _integration_commit,
    _repo,
    _snapshot,
)
from tests.agent.test_bestplan_local_push_state import (
    _seed_local_plan,
    _seed_review_pass,
    _store,
    _target,
)


@dataclass
class _PreparedLanding:
    repo: Path
    snapshot: object
    integration: object
    checks: object
    commands: tuple[object, ...]
    store: BestplanStore
    review_store: ReviewStore
    review_claim: object
    review_target: ReviewTarget
    review_receipt_digest: str
    process_start_id: str


def _prepare_landing(
    tmp_path: Path, *, recovery_identity: bool = False,
) -> _PreparedLanding:
    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    integration_oid, tree_oid = _integration_commit(
        repo,
        path="src/feature.py",
        content=b"REVIEWED = True\n",
    )
    integration, checks, commands = _frozen(
        snapshot,
        integration_oid,
        tree_oid,
    )
    store = _store(tmp_path)
    _seed_local_plan(store, snapshot, integration_oid)
    row = store.get_plan("bp-local")
    assert row is not None
    diff_bytes = b"diff --git a/src/feature.py b/src/feature.py\n+REVIEWED = True\n"
    review_target = ReviewTarget.bestplan_integration(
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
        diff_sha256=hashlib.sha256(diff_bytes).hexdigest(),
        acceptance_digest=hashlib.sha256(b"exact landing acceptance").hexdigest(),
        policy_digest=hashlib.sha256(b"two-slot review policy").hexdigest(),
    )
    review_store, review_claim, review_receipt_digest = _seed_review_pass(
        store, review_target, recovery_identity=recovery_identity,
    )
    prepared = store.prepare_local_push(
        "bp-local",
        session_id="session-1",
        profile="coder",
        workspace=snapshot.repo.workspace,
        expected_target_oid=snapshot.head_oid,
        integration_oid=integration_oid,
        check_set_digest=checks.receipt_digest,
        review_target=review_target,
        review_receipt_digest=review_receipt_digest,
        target=_target(snapshot, integration_oid),
        expires_at=int(time.time()) + 600,
    )
    assert prepared is not None
    start = get_process_start_time(os.getpid())
    assert start is not None
    return _PreparedLanding(
        repo=repo,
        snapshot=snapshot,
        integration=integration,
        checks=checks,
        commands=commands,
        store=store,
        review_store=review_store,
        review_claim=review_claim,
        review_target=review_target,
        review_receipt_digest=review_receipt_digest,
        process_start_id=f"kernel-start:{start}",
    )


def _claim_landing(
    prepared: _PreparedLanding,
    *,
    store: BestplanStore | None = None,
    fencing_token: int | None = None,
    operation_id: str = "claim-landing",
):
    owner = store or prepared.store
    claim = getattr(owner, "claim_landing", None)
    assert callable(claim), (
        "BestplanStore.claim_landing must own the durable landing CAS"
    )
    return claim(
        "bp-local",
        owner_id="review-worker",
        fencing_token=(
            prepared.review_claim.fencing_token
            if fencing_token is None
            else fencing_token
        ),
        owner_pid=os.getpid(),
        owner_process_start_id=prepared.process_start_id,
        operation_id=operation_id,
    )


def _recover_landing(
    store: BestplanStore,
    *,
    owner_is_live,
    observe_local_main,
    now_ns: int,
):
    recover = getattr(store, "recover_landing_claim", None)
    assert callable(recover), (
        "BestplanStore.recover_landing_claim must reconcile without Git replay"
    )
    return recover(
        "bp-local",
        owner_is_live=owner_is_live,
        observe_local_main=observe_local_main,
        now_ns=now_ns,
    )


def _status(value: object) -> str:
    return str(getattr(value, "status", value))


def _lock_probe(lock_path: str | Path) -> subprocess.CompletedProcess[str]:
    script = (
        "import fcntl,os,sys\n"
        "flags=os.O_RDONLY if os.path.isdir(sys.argv[1]) else os.O_RDWR|os.O_CREAT\n"
        "flags|=getattr(os, 'O_DIRECTORY', 0) if os.path.isdir(sys.argv[1]) else 0\n"
        "descriptor=os.open(sys.argv[1], flags)\n"
        "try:\n"
        " fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except BlockingIOError:\n"
        " os.close(descriptor)\n"
        " raise SystemExit(73)\n"
        "os.close(descriptor)\n"
        "raise SystemExit(0)\n"
    )
    return subprocess.run(
        [sys.executable, "-c", script, str(lock_path)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_claim_landing_is_durable_target_bound_and_not_reissued_after_reopen(
    tmp_path,
):
    prepared = _prepare_landing(tmp_path)

    authorization = _claim_landing(prepared)

    assert authorization.plan_id == "bp-local"
    assert authorization.review_job_id == "review-job-local"
    assert authorization.target_digest == prepared.review_target.target_digest
    assert authorization.integration_oid == prepared.integration.integration_oid
    assert authorization.check_receipt_digest == prepared.checks.receipt_digest
    assert authorization.fencing_token == prepared.review_claim.fencing_token
    assert authorization.owner_pid == os.getpid()
    assert authorization.owner_process_start_id == prepared.process_start_id
    assert authorization.repository_id == prepared.snapshot.repo.repository_id
    assert prepared.review_store.get_job("review-job-local").state == (
        "landing_claimed"
    )

    state_path = prepared.store.state_db_path
    prepared.store.close()
    reopened = BestplanStore(db_path=state_path, reconcile_push_state=False)
    assert ReviewStore(state_path).get_job("review-job-local").state == (
        "landing_claimed"
    )
    with pytest.raises(ReviewLeaseConflict, match="landing_already_claimed"):
        _claim_landing(
            prepared,
            store=reopened,
            operation_id="claim-after-reopen",
        )


def test_cancel_after_prepare_wins_before_landing_claim(tmp_path):
    prepared = _prepare_landing(tmp_path)
    child_signals: list[str] = []

    cancelled = prepared.review_store.request_cancel(
        job_id="review-job-local",
        owner_id="review-worker",
        fencing_token=prepared.review_claim.fencing_token,
        operation_id="cancel-before-claim",
        signal_children=lambda: child_signals.append("signalled"),
    )

    assert cancelled.state == "cancel_requested"
    assert cancelled.cancel_requested
    assert child_signals == ["signalled"]
    with pytest.raises(ReviewLeaseConflict, match="cancel"):
        _claim_landing(prepared)
    assert _git(prepared.repo, "rev-parse", "HEAD") == prepared.snapshot.head_oid


def test_expired_review_lease_cannot_claim_landing_before_effect(tmp_path):
    prepared = _prepare_landing(tmp_path)
    with sqlite3.connect(prepared.store.state_db_path) as connection:
        connection.execute(
            "UPDATE review_jobs SET lease_expires_at_ns=0 WHERE job_id=?",
            ("review-job-local",),
        )

    with pytest.raises(ReviewLeaseConflict, match="expired"):
        _claim_landing(prepared, operation_id="expired-lease-claim")

    durable = prepared.review_store.get_job("review-job-local")
    assert durable.state == "landing_prepared"
    assert durable.landing_authorization_digest is None
    assert _git(prepared.repo, "rev-parse", "HEAD") == prepared.snapshot.head_oid


def test_expired_landing_preparation_can_be_reclaimed_with_a_new_fence(tmp_path):
    prepared = _prepare_landing(tmp_path)
    with sqlite3.connect(prepared.store.state_db_path) as connection:
        connection.execute(
            "UPDATE review_jobs SET lease_expires_at_ns=0 WHERE job_id=?",
            ("review-job-local",),
        )
    reclaimed = prepared.review_store.claim_job(
        job_id="review-job-local",
        owner_id="recovery-worker",
        now_ns=time.time_ns(),
        lease_duration_ns=60_000_000_000,
        expected_fencing_token=prepared.review_claim.fencing_token,
    )

    authorization = prepared.store.claim_landing(
        "bp-local",
        owner_id=reclaimed.owner_id,
        fencing_token=reclaimed.fencing_token,
        owner_pid=os.getpid(),
        owner_process_start_id=prepared.process_start_id,
        operation_id="reclaimed-lease-claim",
    )

    assert authorization.fencing_token == reclaimed.fencing_token
    assert reclaimed.fencing_token > prepared.review_claim.fencing_token
    assert prepared.review_store.get_job("review-job-local").state == (
        "landing_claimed"
    )


def test_cancel_and_landing_claim_share_one_serialized_winner(tmp_path):
    prepared = _prepare_landing(tmp_path)
    state_path = prepared.store.state_db_path
    claim_store = BestplanStore(db_path=state_path, reconcile_push_state=False)
    cancel_store = ReviewStore(state_path)
    start = threading.Barrier(3)
    outcomes: dict[str, object] = {}

    def claim_worker() -> None:
        start.wait()
        try:
            outcomes["claim"] = _claim_landing(
                prepared,
                store=claim_store,
                operation_id="racing-claim",
            )
        except BaseException as exc:
            outcomes["claim"] = exc

    def cancel_worker() -> None:
        start.wait()
        try:
            outcomes["cancel"] = cancel_store.request_cancel(
                job_id="review-job-local",
                owner_id="review-worker",
                fencing_token=prepared.review_claim.fencing_token,
                operation_id="racing-cancel",
                signal_children=lambda: None,
            )
        except BaseException as exc:
            outcomes["cancel"] = exc

    threads = [
        threading.Thread(target=claim_worker),
        threading.Thread(target=cancel_worker),
    ]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)

    claim_outcome = outcomes["claim"]
    cancel_outcome = outcomes["cancel"]
    if not isinstance(claim_outcome, BaseException):
        assert isinstance(cancel_outcome, ReviewLeaseConflict)
        assert "landing_already_claimed" in str(cancel_outcome)
        assert cancel_store.get_job("review-job-local").state == "landing_claimed"
    else:
        assert isinstance(claim_outcome, ReviewLeaseConflict)
        assert "cancel" in str(claim_outcome).casefold()
        assert not isinstance(cancel_outcome, BaseException)
        assert cancel_outcome.state == "cancel_requested"
    assert _git(prepared.repo, "rev-parse", "HEAD") == prepared.snapshot.head_oid


def test_land_checked_integration_rejects_a_direct_call_without_authorization(
    tmp_path,
    monkeypatch,
):
    prepared = _prepare_landing(tmp_path)
    monkeypatch.setattr(
        bestplan_local_git,
        "_run_local_git_effect",
        lambda *_args, **_kwargs: pytest.fail(
            "a direct call reached the Git effect without landing authorization"
        ),
    )

    with pytest.raises(LocalMainProofStale, match="landing authorization"):
        bestplan_local_git.land_checked_integration(
            snapshot=prepared.snapshot,
            integration=prepared.integration,
            checks=prepared.checks,
            commands=prepared.commands,
            deadline=time.monotonic() + 20.0,
        )

    assert _git(prepared.repo, "rev-parse", "HEAD") == prepared.snapshot.head_oid


def test_landing_rejects_fabricated_stale_and_old_token_authority(
    tmp_path,
    monkeypatch,
):
    prepared = _prepare_landing(tmp_path)
    monkeypatch.setattr(
        bestplan_local_git,
        "_run_local_git_effect",
        lambda *_args, **_kwargs: pytest.fail(
            "invalid landing authority reached the Git effect"
        ),
    )

    with pytest.raises(ReviewLeaseConflict, match="fencing|token"):
        _claim_landing(
            prepared,
            fencing_token=prepared.review_claim.fencing_token - 1,
            operation_id="old-token-claim",
        )

    fabricated = SimpleNamespace(
        plan_id="bp-local",
        review_job_id="review-job-local",
        target_digest=prepared.review_target.target_digest,
        integration_oid=prepared.integration.integration_oid,
        check_receipt_digest=prepared.checks.receipt_digest,
        fencing_token=prepared.review_claim.fencing_token,
        owner_pid=os.getpid(),
        owner_process_start_id=prepared.process_start_id,
        repository_id=prepared.snapshot.repo.repository_id,
    )
    with pytest.raises(LocalMainProofStale, match="landing authorization"):
        bestplan_local_git.land_checked_integration(
            snapshot=prepared.snapshot,
            integration=prepared.integration,
            checks=prepared.checks,
            commands=prepared.commands,
            authorization=fabricated,
            deadline=time.monotonic() + 20.0,
        )

    authorization = _claim_landing(prepared, operation_id="exact-claim")
    stale_oid, stale_tree = _integration_commit(
        prepared.repo,
        path="src/stale.py",
        content=b"STALE = True\n",
    )
    stale_integration, stale_checks, stale_commands = _frozen(
        prepared.snapshot,
        stale_oid,
        stale_tree,
    )
    with pytest.raises(LocalMainProofStale, match="landing authorization"):
        bestplan_local_git.land_checked_integration(
            snapshot=prepared.snapshot,
            integration=stale_integration,
            checks=stale_checks,
            commands=stale_commands,
            authorization=authorization,
            deadline=time.monotonic() + 20.0,
        )

    assert _git(prepared.repo, "rev-parse", "HEAD") == prepared.snapshot.head_oid


def test_live_landing_owner_is_not_reclaimed_after_lease_expiry(tmp_path):
    prepared = _prepare_landing(tmp_path)
    authorization = _claim_landing(prepared)
    observations: list[str] = []

    result = _recover_landing(
        prepared.store,
        owner_is_live=lambda pid, start_id: (
            pid == authorization.owner_pid
            and start_id == authorization.owner_process_start_id
        ),
        observe_local_main=lambda **_kwargs: observations.append("observed"),
        now_ns=10**18,
    )

    assert _status(result) == "owner_alive"
    assert observations == []
    assert prepared.review_store.get_job("review-job-local").state == (
        "landing_claimed"
    )
    assert _git(prepared.repo, "rev-parse", "HEAD") == prepared.snapshot.head_oid


def test_released_effect_lock_allows_same_process_landing_recovery(tmp_path):
    prepared = _prepare_landing(tmp_path)
    authorization = _claim_landing(
        prepared, operation_id="same-process-mark-failed",
    )
    lock_path = authorization.repository_effect_lock_path
    authorization.release_effect_lock()
    assert _lock_probe(lock_path).returncode == 0
    durable = prepared.review_store.get_job("review-job-local")
    assert durable.landing_operation_active is True
    observations: list[str] = []

    result = _recover_landing(
        prepared.store,
        owner_is_live=lambda pid, start_id: (
            pid == authorization.owner_pid
            and start_id == authorization.owner_process_start_id
        ),
        observe_local_main=lambda **_kwargs: observations.append("expected")
        or "expected",
        now_ns=10**18,
    )

    assert _status(result) == "retry_pre_effect"
    assert observations == ["expected"]
    durable = prepared.review_store.get_job("review-job-local")
    assert durable.state == "landing_prepared"
    assert durable.landing_operation_active is False


def test_held_effect_lock_blocks_recovery_when_pid_probe_says_dead(tmp_path):
    prepared = _prepare_landing(tmp_path)
    authorization = _claim_landing(
        prepared, operation_id="held-effect-dead-pid-probe",
    )
    observations: list[str] = []

    result = _recover_landing(
        prepared.store,
        owner_is_live=lambda _pid, _start_id: False,
        observe_local_main=lambda **_kwargs: observations.append("observed"),
        now_ns=10**18,
    )

    assert _status(result) == "owner_alive"
    assert observations == []
    assert _lock_probe(authorization.repository_effect_lock_path).returncode == 73
    assert prepared.review_store.get_job("review-job-local").state == (
        "landing_claimed"
    )


def test_replacing_legacy_lock_path_cannot_bypass_live_landing_holder(tmp_path):
    prepared = _prepare_landing(tmp_path)
    common_dir = Path(prepared.snapshot.repo.common_dir)
    legacy_lock = common_dir / "hermes-bestplan-landing.lock"
    legacy_lock.write_bytes(b"legacy")
    authorization = _claim_landing(
        prepared, operation_id="replacement-path-adversary",
    )
    displaced = common_dir / "displaced-landing.lock"
    legacy_lock.rename(displaced)
    legacy_lock.write_bytes(b"replacement")
    observations: list[str] = []

    result = _recover_landing(
        prepared.store,
        owner_is_live=lambda _pid, _start_id: False,
        observe_local_main=lambda **_kwargs: observations.append("observed")
        or "expected",
        now_ns=10**18,
    )

    assert _status(result) == "owner_alive"
    assert observations == []
    assert prepared.review_store.get_job("review-job-local").state == (
        "landing_claimed"
    )
    assert authorization.repository_effect_lock_path == str(common_dir)


def test_repository_effect_lock_is_held_through_the_git_effect(
    tmp_path,
    monkeypatch,
):
    prepared = _prepare_landing(tmp_path)
    authorization = _claim_landing(prepared)
    lock_path = getattr(authorization, "repository_effect_lock_path", None)
    assert isinstance(lock_path, str) and lock_path
    assert _lock_probe(lock_path).returncode == 73
    real_effect = bestplan_local_git._run_local_git_effect
    probes: list[int] = []

    def checked_effect(*args, **kwargs):
        probes.append(_lock_probe(lock_path).returncode)
        return real_effect(*args, **kwargs)

    monkeypatch.setattr(
        bestplan_local_git,
        "_run_local_git_effect",
        checked_effect,
    )
    receipt = bestplan_local_git.land_checked_integration(
        snapshot=prepared.snapshot,
        integration=prepared.integration,
        checks=prepared.checks,
        commands=prepared.commands,
        authorization=authorization,
        deadline=time.monotonic() + 20.0,
    )

    assert probes == [73]
    assert isinstance(receipt, LocalMainLandingReceipt)
    assert receipt.new_oid == prepared.integration.integration_oid


def test_interrupted_git_wait_reaps_child_before_landing_lock_is_released(
    tmp_path,
    monkeypatch,
):
    prepared = _prepare_landing(tmp_path)
    authorization = _claim_landing(prepared)
    first_wait_interrupted = threading.Event()
    reap_wait_started = threading.Event()
    child_exited = threading.Event()
    errors: list[BaseException] = []
    unwound: list[bool] = []

    class GatedProcess:
        def __init__(self):
            self.wait_calls = 0

        def wait(self):
            self.wait_calls += 1
            if self.wait_calls == 1:
                first_wait_interrupted.set()
                raise KeyboardInterrupt
            reap_wait_started.set()
            assert child_exited.wait(timeout=5.0)
            return 0

    process = GatedProcess()
    real_popen = subprocess.Popen

    def gated_popen(command, *args, **kwargs):
        if command[0] == "/usr/bin/git":
            return process
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(
        bestplan_local_git.subprocess,
        "Popen",
        gated_popen,
    )

    def run_effect_and_unwind() -> None:
        try:
            bestplan_local_git._run_uninterruptible_git_effect(
                prepared.snapshot,
                ("merge", "--ff-only", prepared.integration.integration_oid),
            )
        except bestplan_local_git.LocalMainEffectUnknown as exc:
            errors.append(exc)
            unwound.append(
                prepared.store.mark_landing_observation_pending(
                    "bp-local", authorization=authorization,
                )
            )

    worker = threading.Thread(target=run_effect_and_unwind)
    worker.start()
    assert first_wait_interrupted.wait(timeout=2.0)
    try:
        assert reap_wait_started.wait(timeout=2.0)
        assert worker.is_alive()
        durable = prepared.review_store.get_job("review-job-local")
        assert durable.landing_operation_active is True
        assert _lock_probe(authorization.repository_effect_lock_path).returncode == 73
    finally:
        child_exited.set()
        worker.join(timeout=5.0)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert unwound == [True]
    assert prepared.review_store.get_job(
        "review-job-local"
    ).landing_operation_active is False
    assert _lock_probe(authorization.repository_effect_lock_path).returncode == 0


def test_dead_owner_recovery_observes_under_lock_and_never_replays_git(
    tmp_path,
    monkeypatch,
):
    prepared = _prepare_landing(tmp_path)
    authorization = _claim_landing(prepared)
    lock_path = authorization.repository_effect_lock_path
    _git(
        prepared.repo,
        "merge",
        "--ff-only",
        prepared.integration.integration_oid,
    )
    assert _git(prepared.repo, "rev-parse", "HEAD") == (
        prepared.integration.integration_oid
    )
    state_path = prepared.store.state_db_path
    prepared.store.close()
    del authorization
    gc.collect()
    assert _lock_probe(lock_path).returncode == 0
    replay_calls: list[str] = []
    monkeypatch.setattr(
        bestplan_local_git,
        "land_checked_integration",
        lambda **_kwargs: replay_calls.append("replayed")
        or pytest.fail("dead-owner recovery must never replay Git"),
    )
    observations: list[str] = []

    def observe_local_main(**kwargs):
        observations.append("observed")
        assert _lock_probe(lock_path).returncode == 73
        return bestplan_local_git.classify_local_main_for_push(**kwargs)

    reopened = BestplanStore(db_path=state_path, reconcile_push_state=False)
    result = _recover_landing(
        reopened,
        owner_is_live=lambda _pid, _start_id: False,
        observe_local_main=observe_local_main,
        now_ns=10**18,
    )

    assert _status(result) == "landed"
    assert observations == ["observed"]
    assert replay_calls == []
    assert _git(prepared.repo, "rev-parse", "HEAD") == (
        prepared.integration.integration_oid
    )
    assert reopened.get_plan("bp-local")["local_push_state"] == "awaiting"


def test_dead_pre_effect_landing_claim_is_released_for_the_same_pass(tmp_path):
    prepared = _prepare_landing(tmp_path)
    authorization = prepared.store.claim_landing(
        "bp-local",
        owner_id="review-worker",
        fencing_token=prepared.review_claim.fencing_token,
        owner_pid=999_999_999,
        owner_process_start_id="kernel-start:1",
        operation_id="dead-pre-effect-claim",
    )
    lock_path = authorization.repository_effect_lock_path
    del authorization
    gc.collect()
    assert _lock_probe(lock_path).returncode == 0

    result = _recover_landing(
        prepared.store,
        owner_is_live=lambda _pid, _start_id: False,
        observe_local_main=lambda **_kwargs: "expected",
        now_ns=10**18,
    )

    assert _status(result) == "retry_pre_effect"
    durable = prepared.review_store.get_job("review-job-local")
    assert durable.state == "landing_prepared"
    assert durable.owner_id is None
    assert durable.lease_expires_at_ns is None
    assert durable.landing_owner_pid is None
    assert durable.landing_authorization_digest is None
    assert prepared.store.get_plan("bp-local")["local_push_state"] == "prepared"
    assert _git(prepared.repo, "rev-parse", "HEAD") == prepared.snapshot.head_oid


def test_finished_same_process_landing_operation_is_observed_not_pid_blocked(
    tmp_path,
):
    prepared = _prepare_landing(tmp_path)
    authorization = _claim_landing(
        prepared, operation_id="same-process-effect-unwound",
    )
    assert prepared.store.mark_landing_observation_pending(
        "bp-local", authorization=authorization,
    )
    assert _lock_probe(authorization.repository_effect_lock_path).returncode == 0
    observations: list[str] = []

    unavailable = _recover_landing(
        prepared.store,
        owner_is_live=lambda _pid, _start_id: True,
        observe_local_main=lambda **_kwargs: observations.append("unavailable")
        or (_ for _ in ()).throw(RuntimeError("read-back unavailable")),
        now_ns=10**18,
    )
    released = _recover_landing(
        prepared.store,
        owner_is_live=lambda _pid, _start_id: True,
        observe_local_main=lambda **_kwargs: observations.append("expected")
        or "expected",
        now_ns=10**18,
    )

    assert _status(unavailable) == "observation_unavailable"
    assert _status(released) == "retry_pre_effect"
    assert observations == ["unavailable", "expected"]
    durable = prepared.review_store.get_job("review-job-local")
    assert durable.state == "landing_prepared"
    assert durable.landing_operation_active is False


def test_activation_failure_releases_effect_for_same_process_recovery(
    monkeypatch,
):
    from tools import delegate_tool

    calls = []

    class Authorization:
        authorization_digest = "authorization"

        def release_effect_lock(self):
            calls.append("released")

    authorization = Authorization()

    class Store:
        def __init__(self, **_kwargs):
            pass

        def prepare_local_push(self, *_args, **_kwargs):
            return {"state": "prepared"}

        def claim_landing(self, *_args, **_kwargs):
            return authorization

        def activate_local_push(self, *_args, **_kwargs):
            calls.append("activate-failed")
            return False

        def mark_landing_observation_pending(self, *_args, **_kwargs):
            calls.append("observation-pending")
            authorization.release_effect_lock()
            return True

        def close(self):
            calls.append("closed")

    monkeypatch.setattr("agent.bestplan_state.BestplanStore", Store)
    monkeypatch.setattr(
        bestplan_local_git,
        "observe_prelanding_local_main_push_target",
        lambda **_kwargs: SimpleNamespace(
            display_url="local", remote_ref="refs/heads/main",
        ),
    )
    monkeypatch.setattr(
        bestplan_local_git,
        "land_checked_integration",
        lambda **_kwargs: LocalMainLandingReceipt(
            target_ref="refs/heads/main",
            old_oid="1" * 40,
            new_oid="2" * 40,
            check_receipt_digest="3" * 64,
            authorization_digest="authorization",
        ),
    )
    monkeypatch.setattr(
        "gateway.status.get_process_start_time", lambda _pid: 1,
    )
    reviewed = delegate_tool._LocalBestplanReviewResult(
        integration=SimpleNamespace(
            target_oid="1" * 40, integration_oid="2" * 40,
        ),
        checks=SimpleNamespace(receipt_digest="3" * 64),
        target=SimpleNamespace(generation=0),
        receipt=SimpleNamespace(receipt_digest="4" * 64, passed=True),
        job_id="review-job-local",
        owner_id="review-worker",
        fencing_token=1,
    )

    with pytest.raises(bestplan_local_git.LocalMainEffectUnknown):
        delegate_tool._land_reviewed_local_bestplan(
            plan_id="bp-local",
            snapshot=SimpleNamespace(
                repo=SimpleNamespace(workspace="/workspace"),
            ),
            runtime=SimpleNamespace(
                check_plan=SimpleNamespace(commands=()),
            ),
            state_db_path=Path("/state.db"),
            session_id="session-1",
            profile="coder",
            reviewed=reviewed,
            projected_results=({"status": "frozen"},),
            deadline=time.monotonic() + 10,
            cancel_event=None,
        )

    assert calls == [
        "activate-failed", "observation-pending", "released", "closed",
    ]


def test_startup_reconcile_finishes_dead_landing_claim_without_replaying_git(
    tmp_path,
    monkeypatch,
):
    prepared = _prepare_landing(tmp_path)
    authorization = prepared.store.claim_landing(
        "bp-local",
        owner_id="review-worker",
        fencing_token=prepared.review_claim.fencing_token,
        owner_pid=999_999_999,
        owner_process_start_id="kernel-start:1",
        operation_id="dead-owner-claim",
    )
    _git(
        prepared.repo,
        "merge",
        "--ff-only",
        prepared.integration.integration_oid,
    )
    state_path = prepared.store.state_db_path
    lock_path = authorization.repository_effect_lock_path
    prepared.store.close()
    del authorization
    gc.collect()
    assert _lock_probe(lock_path).returncode == 0
    monkeypatch.setattr(
        bestplan_local_git,
        "land_checked_integration",
        lambda **_kwargs: pytest.fail("startup recovery replayed the Git effect"),
    )

    reopened = BestplanStore(db_path=state_path, reconcile_push_state=False)
    changed = reopened.reconcile_local_pushes()

    assert changed == 1
    assert ReviewStore(state_path).get_job("review-job-local").state == "landed"
    assert reopened.get_plan("bp-local")["local_push_state"] == "awaiting"
    assert _git(prepared.repo, "rev-parse", "HEAD") == (
        prepared.integration.integration_oid
    )


def test_activation_rejects_a_fabricated_receipt_without_a_landing_claim(
    tmp_path,
):
    prepared = _prepare_landing(tmp_path)

    activated = prepared.store.activate_local_push(
        "bp-local",
        landing_receipt=LocalMainLandingReceipt(
            target_ref="refs/heads/main",
            old_oid=prepared.snapshot.head_oid,
            new_oid=prepared.integration.integration_oid,
            check_receipt_digest=prepared.checks.receipt_digest,
        ),
    )

    assert not activated
    assert prepared.store.get_plan("bp-local")["local_push_state"] == "prepared"
    assert _git(prepared.repo, "rev-parse", "HEAD") == prepared.snapshot.head_oid
