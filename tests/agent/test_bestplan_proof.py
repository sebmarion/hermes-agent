from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent.bestplan_contract import (
    BlockingReview,
    BoundCommand,
    ControllerIdentity,
    EnrolledRepository,
    Enrollment,
    LiveTarget,
    PinnedInput,
    Publication,
    RollbackTarget,
    approval_digest,
    build_execution_contract,
    contract_digest,
    contract_json,
    source_snapshot_digest,
    source_snapshot_json,
)
from agent.bestplan_source import (
    IndexEntry,
    IndexFlags,
    ProtectedManifest,
    RepoIdentity,
    SourceSnapshot,
)
from agent.bestplan_state import (
    BESTPLAN_ENVELOPE_END,
    BESTPLAN_ENVELOPE_START,
    BestplanStore,
    PlanState,
    _manifest_digest,
    try_resolve_go,
)
from agent.execution_plan import compile_execution_plan


def _proof():
    return importlib.import_module("agent.bestplan_proof")


def _store(path: Path) -> BestplanStore:
    return BestplanStore(db_path=path)


def _command(identifier: str) -> BoundCommand:
    return BoundCommand(
        identifier=identifier,
        executable="/usr/bin/python3",
        executable_sha256="9" * 64,
        argv=("-m", "pytest", "-q"),
        logical_cwd="integration",
        env=(("PYTHONHASHSEED", "0"),),
        inputs=(PinnedInput("pyproject.toml", "a" * 64),),
        cache=(),
        timeout_seconds=600,
        network_allowlist=(),
    )


def _plan(workspace: str = "/tmp/proof-work"):
    workspace = str(Path(workspace).resolve())
    return compile_execution_plan(
        {
            "version": 1,
            "mode": "delegate",
            "risk": "low",
            "slices": [
                {
                    "id": "code",
                    "kind": "implement",
                    "goal": "Make the bounded change",
                    "depends_on": [],
                    "capability": "fast_fallback",
                    "workspace": workspace,
                    "allowed_paths": ["agent/"],
                    "read_only": False,
                    "expected_artifacts": ["agent/change.py"],
                    "acceptance": ["focused checks pass"],
                }
            ],
            "merge_policy": "Integrate only after checks.",
            "stop_condition": "Acceptance passes.",
            "escalation_predicates": ["review_required"],
        }
    )


def _repo(workspace: str = "/tmp/proof-work") -> RepoIdentity:
    workspace = str(Path(workspace).resolve())
    common = "/tmp/proof-repo/.git"
    return RepoIdentity(
        workspace=workspace,
        workspace_raw=workspace.encode(),
        worktree=workspace,
        worktree_raw=workspace.encode(),
        git_dir=common,
        git_dir_raw=common.encode(),
        common_dir=common,
        common_dir_raw=common.encode(),
        common_dir_device=11,
        common_dir_inode=22,
        object_format="sha1",
        repository_id="proof-repository",
    )


def _snapshot(repo: RepoIdentity | None = None) -> SourceSnapshot:
    repo = repo or _repo()
    manifest = ProtectedManifest(
        index_entries=(IndexEntry(b"agent/base.py", 0o100644, "1" * 40, 0),),
        index_flags=(
            IndexFlags(b"agent/base.py", b"H ", b"", False, False, False, False),
        ),
        worktree_entries=(),
        protected_paths=(b"agent/base.py",),
        staged_diff_sha256="2" * 64,
        unstaged_diff_sha256="3" * 64,
        digest="4" * 64,
    )
    return SourceSnapshot(
        repo=repo,
        head_symbolic=True,
        head_ref=b"refs/heads/main",
        head_raw=b"ref: refs/heads/main\n",
        head_oid="5" * 40,
        tree_oid="6" * 40,
        protected_manifest=manifest,
        capture_implementation_sha256="7" * 64,
        fingerprint="8" * 64,
    )


def _enrollment(repo: RepoIdentity | None = None) -> Enrollment:
    repo = repo or _repo()
    rollback = RollbackTarget(
        repository_id=repo.repository_id,
        selector="/var/db/hermes/releases/current",
        service="com.nous.hermes.gateway",
        command=_command("rollback"),
    )
    live = LiveTarget(
        repository_id=repo.repository_id,
        adapter="launchd",
        target_id="gateway-primary",
        service="com.nous.hermes.gateway",
        activation=_command("activate"),
        health=_command("health"),
        canary=_command("canary"),
        rollback=rollback,
    )
    return Enrollment(
        reference="prod-gateway",
        enrollment_id="enrollment-1",
        revision=7,
        epoch="epoch-3",
        repository=EnrolledRepository.from_repo_identity(repo),
        source_policy="head_only",
        capture_budget_seconds=30,
        local_ref="refs/heads/main",
        publication=Publication(
            repository_id=repo.repository_id,
            remote_name="origin",
            push_url="https://github.com/example/hermes-agent.git",
            remote_ref="refs/heads/main",
            observed_oid="c" * 40,
        ),
        commands=(_command("focused-tests"),),
        review=BlockingReview(
            lane="smart_reviewer",
            command=_command("review"),
            blocking_severities=("critical", "high"),
        ),
        live_targets=(live,),
        controller=ControllerIdentity(
            repository_id=repo.repository_id,
            controller_id="controller-c0",
            release_oid="d" * 40,
            artifact_sha256="e" * 64,
        ),
        promotion_mode="auto_live",
    )


def _insert_v2_plan(
    store: BestplanStore,
    *,
    plan_id: str = "bp_proof",
    state: str = PlanState.RUNNING,
    source_present: bool = True,
) -> dict:
    plan = _plan()
    snapshot = _snapshot()
    contract = build_execution_contract(plan, snapshot, _enrollment(), _enrollment().controller)
    manifest = plan.to_manifest()
    envelope = (
        f"{BESTPLAN_ENVELOPE_START}\n"
        + json.dumps({"version": 1, "manifest": manifest}, sort_keys=True)
        + f"\n{BESTPLAN_ENVELOPE_END}"
    )
    source_json = source_snapshot_json(snapshot) if source_present else None
    source_digest = source_snapshot_digest(snapshot) if source_present else None

    def insert(conn):
        conn.execute(
            """INSERT INTO bestplan_plans (
                plan_id, version, created_at, session_id, profile, workspace,
                baseline_revision, baseline_fingerprint, raw_request,
                raw_plan_json, validated_manifest_json, state, approval_digest,
                execution_protocol, promotion_contract_version,
                promotion_contract_json, promotion_contract_digest,
                promotion_mode, source_snapshot_json, source_snapshot_digest,
                current_phase
            ) VALUES (?, 1, 1, 'session', 'coder', ?, ?, ?, 'request', ?, ?, ?, ?,
                      2, 2, ?, ?, 'auto_live', ?, ?, 'captured')""",
            (
                plan_id,
                snapshot.repo.workspace,
                snapshot.head_oid,
                snapshot.fingerprint,
                envelope,
                json.dumps(manifest, sort_keys=True),
                state,
                approval_digest(manifest, contract),
                contract_json(contract),
                contract_digest(contract),
                source_json,
                source_digest,
            ),
        )

    store._execute_write(insert)
    return store.get_plan(plan_id)


def _insert_v1_plan(
    store: BestplanStore,
    *,
    plan_id: str = "bp_legacy",
    state: str = PlanState.COMPLETED_UNVERIFIED,
) -> None:
    plan = _plan()
    workspace = str(Path("/tmp/proof-work").resolve())
    manifest = plan.to_manifest()
    envelope = (
        f"{BESTPLAN_ENVELOPE_START}\n"
        + json.dumps({"version": 1, "manifest": manifest}, sort_keys=True)
        + f"\n{BESTPLAN_ENVELOPE_END}"
    )
    store._execute_write(
        lambda conn: conn.execute(
            """INSERT INTO bestplan_plans (
                plan_id, version, created_at, session_id, profile, workspace,
                baseline_fingerprint, raw_request, raw_plan_json,
                validated_manifest_json, state, approval_digest, execution_protocol
            ) VALUES (?, 1, 1, 'session', 'coder', ?, 'base',
                      'request', ?, ?, ?, ?, 1)""",
            (
                plan_id,
                workspace,
                envelope,
                json.dumps(manifest, sort_keys=True),
                state,
                _manifest_digest(manifest),
            ),
        )
    )


def _task5_go_context(tmp_path: Path, monkeypatch) -> dict[str, object]:
    """Supply the retained host bindings that protocol 2 now requires."""
    from tools import delegate_tool

    runtime = delegate_tool.BestplanHostRuntime(
        controller=_enrollment().controller,
        controller_source=tmp_path / "retained-controller",
        controller_python=tmp_path / "retained-python" / "bin" / "python3.11",
        runtime_read_paths=(),
        attempts_root=tmp_path / "attempts",
        policy_version=1,
        request_budget=4,
        token_budget=8192,
        max_iterations=8,
        max_output_tokens=1024,
        timeout_seconds=10,
        capability_ttl_seconds=60,
    )
    # These tests characterize proof/status/redaction behavior.  The real retained
    # artifact and interpreter relationship is exercised by the Task 5 host-ingress
    # tests, so keep this fixture focused on the mandatory handoff shape.
    monkeypatch.setattr(
        delegate_tool,
        "_validate_bestplan_host_runtime",
        lambda *_args, **_kwargs: None,
    )
    return {
        "candidate_host_runtime": runtime,
        "authority_client": object(),
    }


def _v2_runtime() -> dict[str, object]:
    return {
        "route": "code_worker",
        "provider": "test",
        "model": "coder",
        "runtime_fingerprint": "f" * 64,
    }


def _task5_stored_runtime(go_context: dict[str, object]) -> dict[str, object]:
    from tools import delegate_tool

    return {
        **_v2_runtime(),
        "toolsets": ["file"],
        "bestplan_toolsets": ["file"],
        **delegate_tool._bestplan_host_runtime_projection(
            go_context["candidate_host_runtime"]
        ),
    }


def _operation(number: int) -> str:
    return str(uuid.UUID(int=number))


def _prepare_dispatching(store: BestplanStore) -> None:
    row = store.get_plan("bp_proof")
    claimed = store.prepare_dispatch_intent(
        "bp_proof",
        row["baseline_fingerprint"],
        resolved_runtimes=[],
        session_id="session",
        profile="coder",
        workspace=row["workspace"],
    )
    assert claimed is not None
    assert store.begin_dispatch_attempt("bp_proof") is True


def _append(
    ledger,
    *,
    plan_id: str = "bp_proof",
    operation: int = 1,
    expected_seq: int = 0,
    expected_hash: str | None = None,
    kind: str = "candidate_ready",
    phase: str = "candidate_ready",
    state: str = PlanState.RUNNING,
    origin: str = "promoter",
    raw_output=None,
    integration_oid: str | None = None,
    artifact_digest: str | None = None,
    contract_digest_value: str | None = None,
    created_at_ns: int = 1_000_000_000,
    ensure_candidate: bool = True,
):
    if expected_seq == 0 and ensure_candidate:
        _candidate(ledger)
    return ledger.append_event(
        plan_id=plan_id,
        authority_epoch="epoch-3",
        operation_id=_operation(operation),
        expected_epoch=None if expected_seq == 0 else "epoch-3",
        expected_seq=expected_seq,
        expected_hash=expected_hash,
        kind=kind,
        phase=phase,
        projected_state=state,
        integration_oid=integration_oid,
        artifact_digest=artifact_digest,
        origin=origin,
        raw_output={"status": "ok"} if raw_output is None else raw_output,
        output_source="check",
        contract_digest=contract_digest_value,
        created_at_ns=created_at_ns,
    )


def test_event_hashes_are_deterministic_domain_separated_and_chain_exactly(tmp_path):
    proof = _proof()
    left_store = _store(tmp_path / "left.db")
    right_store = _store(tmp_path / "right.db")
    _insert_v2_plan(left_store)
    _insert_v2_plan(right_store)

    left = _append(proof.ProofLedger(left_store))
    right = _append(proof.ProofLedger(right_store))

    assert left == right
    assert left.event_hash == right.event_hash
    assert left.event_hash != hashlib.sha256(left.payload_json.encode()).hexdigest()
    second = _append(
        proof.ProofLedger(left_store),
        operation=2,
        expected_seq=left.event_seq,
        expected_hash=left.event_hash,
        kind="integrated_proven",
        phase="integrated_proven",
        integration_oid="a" * 40,
        created_at_ns=2_000_000_000,
    )
    assert second.previous_hash == left.event_hash
    events = proof.ProofLedger(left_store).read_events("bp_proof")
    assert events == [left, second]
    assert proof.ProofLedger(left_store).verify_chain("bp_proof") is True


def test_projection_tracks_exact_phase_oid_state_and_separate_phase_time(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "state.db")
    _insert_v2_plan(store)
    ledger = proof.ProofLedger(store)
    candidate = _append(ledger, created_at_ns=5_000_000_000)
    integrated = _append(
        ledger,
        operation=2,
        expected_seq=candidate.event_seq,
        expected_hash=candidate.event_hash,
        kind="integrated_proven",
        phase="integrated_proven",
        integration_oid="a" * 40,
        created_at_ns=6_000_000_000,
    )
    receipt = _append(
        ledger,
        operation=3,
        expected_seq=integrated.event_seq,
        expected_hash=integrated.event_hash,
        kind="tests_verified",
        phase="tests_verified",
        integration_oid="a" * 40,
        created_at_ns=7_000_000_000,
    )

    row = store.get_plan("bp_proof")
    assert row["current_phase"] == "tests_verified"
    assert row["state"] == PlanState.RUNNING
    assert row["integration_oid"] == "a" * 40
    assert row["artifact_digest"] is None
    assert row["proof_authority_epoch"] == "epoch-3"
    assert row["proof_event_seq"] == receipt.event_seq
    assert row["proof_event_hash"] == receipt.event_hash
    assert row["tests_verified_at"] == 7.0
    assert row["verified_at"] is None

    with pytest.raises(sqlite3.IntegrityError):
        store._execute_write(
            lambda conn: conn.execute(
                "UPDATE bestplan_plans SET current_phase='review_verified' "
                "WHERE plan_id='bp_proof'"
            )
        )


def test_operation_retry_is_idempotent_before_head_check_and_conflicts_on_reuse(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "state.db")
    _insert_v2_plan(store)
    ledger = proof.ProofLedger(store)
    first = _append(ledger)

    assert _append(ledger) == first
    with pytest.raises(proof.ProofOperationConflict):
        _append(ledger, raw_output={"status": "different"})
    assert len(ledger.read_events("bp_proof")) == 1


def test_candidate_ready_clock_timestamp_retry_reuses_stored_event_time(
    tmp_path, monkeypatch,
):
    proof = _proof()
    store = _store(tmp_path / "state.db")
    _insert_v2_plan(store)
    ledger = proof.ProofLedger(store)

    first = _append(ledger, created_at_ns=None)
    monkeypatch.setattr(
        proof.time,
        "time_ns",
        lambda: (_ for _ in ()).throw(
            AssertionError("an exact authority retry must reuse its stored timestamp")
        ),
    )

    assert _append(ledger, created_at_ns=None) == first
    with pytest.raises(proof.ProofOperationConflict):
        _append(ledger, created_at_ns=first.created_at_ns + 1)
    assert len(ledger.read_events("bp_proof")) == 1


def test_idempotent_retry_revalidates_the_stored_receipt_before_returning(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "state.db")
    _insert_v2_plan(store)
    ledger = proof.ProofLedger(store)
    first = _append(ledger)
    store._execute_write(
        lambda conn: (
            conn.execute("DROP TRIGGER bestplan_proof_events_no_update_v1"),
            conn.execute(
                "UPDATE bestplan_proof_events SET event_hash=? "
                "WHERE plan_id='bp_proof' AND operation_id=?",
                ("f" * 64, first.operation_id),
            ),
        )
    )

    with pytest.raises(proof.ProofValidationError, match="stored proof event"):
        _append(ledger)


def test_authority_phase_skips_and_regressions_are_rejected(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "state.db")
    _insert_v2_plan(store)
    ledger = proof.ProofLedger(store)

    first = _append(ledger)
    with pytest.raises(proof.ProofValidationError, match="phase edge"):
        _append(
            ledger,
            operation=2,
            expected_seq=first.event_seq,
            expected_hash=first.event_hash,
            kind="review_verified",
            phase="review_verified",
            integration_oid="a" * 40,
        )
    integrated = _append(
        ledger,
        operation=3,
        expected_seq=first.event_seq,
        expected_hash=first.event_hash,
        kind="integrated_proven",
        phase="integrated_proven",
        integration_oid="a" * 40,
    )
    with pytest.raises(proof.ProofValidationError, match="phase edge"):
        _append(
            ledger,
            operation=4,
            expected_seq=integrated.event_seq,
            expected_hash=integrated.event_hash,
            kind="candidate_ready",
            phase="candidate_ready",
            integration_oid="a" * 40,
        )
    assert store.get_plan("bp_proof")["current_phase"] == "integrated_proven"


def test_authority_append_requires_running_projection_and_monotonic_time(tmp_path):
    proof = _proof()
    pending_store = _store(tmp_path / "pending-authority.db")
    _insert_v2_plan(pending_store, state=PlanState.PENDING)
    with pytest.raises(proof.ProofValidationError, match="state"):
        _append(
            proof.ProofLedger(pending_store),
            state=PlanState.PENDING,
            created_at_ns=10_000_000_000,
        )
    assert pending_store.get_plan("bp_proof")["current_phase"] == "captured"

    store = _store(tmp_path / "authority-time.db")
    _insert_v2_plan(store)
    ledger = proof.ProofLedger(store)
    first = _append(ledger, created_at_ns=10_000_000_000)
    with pytest.raises(proof.ProofValidationError, match="timestamp"):
        _append(
            ledger,
            operation=2,
            expected_seq=first.event_seq,
            expected_hash=first.event_hash,
            kind="integrated_proven",
            phase="integrated_proven",
            integration_oid="a" * 40,
            created_at_ns=9_000_000_000,
        )
    assert store.get_plan("bp_proof")["proof_event_seq"] == first.event_seq


def test_artifact_identity_appears_only_at_explicit_frozen_milestone(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "artifact.db")
    _insert_v2_plan(store)
    ledger = proof.ProofLedger(store)
    previous = None
    for number, phase in enumerate(
        ("candidate_ready", "integrated_proven", "tests_verified", "review_verified"),
        start=1,
    ):
        previous = _append(
            ledger,
            operation=number,
            expected_seq=0 if previous is None else previous.event_seq,
            expected_hash=None if previous is None else previous.event_hash,
            kind=phase,
            phase=phase,
            integration_oid=None if phase == "candidate_ready" else "a" * 40,
        )
        assert previous.artifact_digest is None
    with pytest.raises(proof.ProofValidationError):
        _append(
            ledger,
            operation=5,
            expected_seq=previous.event_seq,
            expected_hash=previous.event_hash,
            kind="main_fast_forwarded",
            phase="main_fast_forwarded",
            integration_oid="a" * 40,
            artifact_digest="b" * 64,
        )
    frozen = _append(
        ledger,
        operation=6,
        expected_seq=previous.event_seq,
        expected_hash=previous.event_hash,
        kind="artifact_frozen",
        phase="artifact_frozen",
        integration_oid="a" * 40,
        artifact_digest="b" * 64,
    )
    assert frozen.artifact_digest == "b" * 64


def test_event_hash_binds_collision_proof_raw_type_framing(tmp_path):
    proof = _proof()
    bytes_store = _store(tmp_path / "bytes.db")
    text_store = _store(tmp_path / "text.db")
    _insert_v2_plan(bytes_store)
    _insert_v2_plan(text_store)

    byte_event = _append(proof.ProofLedger(bytes_store), raw_output=b"same")
    text_event = _append(proof.ProofLedger(text_store), raw_output="same")

    assert byte_event.raw_output_sha256 == text_event.raw_output_sha256
    assert byte_event.raw_output_kind == "bytes"
    assert text_event.raw_output_kind == "string"
    assert byte_event.raw_output_framed_sha256 != text_event.raw_output_framed_sha256
    assert byte_event.event_hash != text_event.event_hash


def test_wrong_expected_head_is_rejected_instead_of_last_writer_wins(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "state.db")
    _insert_v2_plan(store)
    ledger = proof.ProofLedger(store)
    _append(ledger)

    with pytest.raises(proof.ProofHeadMismatch):
        _append(ledger, operation=2)
    assert len(ledger.read_events("bp_proof")) == 1


def test_concurrent_writers_use_exact_head_cas(tmp_path):
    proof = _proof()
    db_path = tmp_path / "state.db"
    bootstrap = _store(db_path)
    _insert_v2_plan(bootstrap)
    bootstrap.close()
    barrier = threading.Barrier(2)

    def write(number: int):
        store = _store(db_path)
        try:
            barrier.wait(timeout=5)
            return _append(proof.ProofLedger(store), operation=number)
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(write, number) for number in (1, 2)]
        outcomes = []
        for future in results:
            try:
                outcomes.append(future.result(timeout=20))
            except proof.ProofHeadMismatch as exc:
                outcomes.append(exc)

    assert sum(isinstance(item, proof.ProofEventReceipt) for item in outcomes) == 1
    assert sum(isinstance(item, proof.ProofHeadMismatch) for item in outcomes) == 1
    check = _store(db_path)
    assert len(proof.ProofLedger(check).read_events("bp_proof")) == 1


def test_event_and_candidate_rows_are_immutable_even_via_direct_sql(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "state.db")
    _insert_v2_plan(store)
    ledger = proof.ProofLedger(store)
    _append(ledger)
    candidate = ledger.record_candidate(
        plan_id="bp_proof",
        candidate_id="candidate-1",
        slice_id="code",
        attempt_id="attempt-1",
        commit_oid="c" * 40,
        tree_oid="d" * 40,
        raw_receipt={"status": "frozen"},
        created_at_ns=1,
    )
    assert candidate.receipt_digest
    plan = store.get_plan("bp_proof")
    candidate_body = json.loads(candidate.receipt_json)
    assert candidate.base_oid == plan["baseline_revision"]
    assert candidate.approval_digest == plan["approval_digest"]
    assert candidate.contract_digest == plan["promotion_contract_digest"]
    assert candidate.source_snapshot_digest == plan["source_snapshot_digest"]
    assert candidate_body["base_oid"] == plan["baseline_revision"]
    assert candidate_body["source_snapshot_digest"] == plan["source_snapshot_digest"]
    assert candidate.raw_output_kind == "mapping"
    assert candidate.raw_output_framed_sha256

    for statement in (
        "UPDATE bestplan_proof_events SET phase='changed' WHERE plan_id='bp_proof'",
        "DELETE FROM bestplan_proof_events WHERE plan_id='bp_proof'",
        "UPDATE bestplan_candidates SET commit_oid='eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee' "
        "WHERE plan_id='bp_proof'",
        "DELETE FROM bestplan_candidates WHERE plan_id='bp_proof'",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            store._execute_write(lambda conn, sql=statement: conn.execute(sql))


def test_candidate_set_is_frozen_bound_to_authority_chain_and_replace_safe(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "candidates.db")
    _insert_v2_plan(store)
    ledger = proof.ProofLedger(store)
    candidate = _candidate(ledger)
    original = dict(
        store._connection()
        .execute(
            "SELECT * FROM bestplan_candidates WHERE plan_id='bp_proof' "
            "AND candidate_id='candidate-1'"
        )
        .fetchone()
    )
    with pytest.raises(sqlite3.IntegrityError):
        store._execute_write(
            lambda conn: conn.execute(
                "INSERT OR REPLACE INTO bestplan_candidates "
                "SELECT * FROM bestplan_candidates WHERE plan_id='bp_proof' "
                "AND candidate_id='candidate-1'"
            )
        )
    assert dict(
        store._connection()
        .execute(
            "SELECT * FROM bestplan_candidates WHERE plan_id='bp_proof' "
            "AND candidate_id='candidate-1'"
        )
        .fetchone()
    ) == original

    first = _append(ledger)
    candidate_set_digest = store.get_plan("bp_proof")["candidate_set_digest"]
    assert candidate_set_digest
    assert first.candidate_set_digest == candidate_set_digest
    with pytest.raises(proof.ProofValidationError):
        ledger.record_candidate(
            plan_id="bp_proof",
            candidate_id="candidate-2",
            slice_id="code",
            attempt_id="attempt-2",
            commit_oid="e" * 40,
            tree_oid="f" * 40,
            raw_receipt={"status": "frozen"},
            created_at_ns=2,
        )
    assert candidate.receipt_digest


def test_v2_direct_terminal_flip_and_legacy_setter_are_blocked_but_v1_works(tmp_path):
    store = _store(tmp_path / "state.db")
    _insert_v2_plan(store, state=PlanState.COMPLETED_UNVERIFIED)
    _insert_v1_plan(store)

    with pytest.raises(sqlite3.IntegrityError):
        store._execute_write(
            lambda conn: conn.execute(
                "UPDATE bestplan_plans SET state='completed_verified', verified_at=1 "
                "WHERE plan_id='bp_proof'"
            )
        )
    assert store.mark_completed_verified("bp_proof") is False
    assert store.mark_completed_verified("bp_legacy") is True
    assert store.get_plan("bp_legacy")["state"] == PlanState.COMPLETED_VERIFIED


def test_protocol2_legacy_atomic_claim_and_state_only_running_transition_are_blocked(
    tmp_path,
):
    store = _store(tmp_path / "legacy-atomic-claim.db")
    row = _insert_v2_plan(store, state=PlanState.APPROVED)
    before = store.get_plan("bp_proof")

    assert store.atomic_claim_approved(
        "bp_proof",
        row["baseline_fingerprint"],
    ) is None
    assert store.get_plan("bp_proof") == before
    with pytest.raises(sqlite3.IntegrityError):
        store._execute_write(
            lambda conn: conn.execute(
                "UPDATE bestplan_plans SET state='running' WHERE plan_id='bp_proof'"
            )
        )
    assert store.get_plan("bp_proof") == before

    _insert_v1_plan(store, plan_id="bp_legacy", state=PlanState.APPROVED)
    claimed = store.atomic_claim_approved("bp_legacy", "base")
    assert claimed is not None
    assert claimed["state"] == PlanState.RUNNING


def test_protocol2_async_terminal_is_redacted_advisory_and_requires_recapture(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "state.db")
    _insert_v2_plan(store)
    secret = "async-terminal-secret-value"
    evidence = {
        "status": "completed",
        "authorization": f"Bearer {secret}",
        "result": {"password": secret, "ordinary": "ok"},
    }

    assert store.mark_completed_unverified("bp_proof", evidence) is True
    assert store.mark_completed_unverified("bp_proof", evidence) is True
    row = store.get_plan("bp_proof")
    events = proof.ProofLedger(store).read_events("bp_proof")

    assert row["state"] == PlanState.RUNNING
    assert row["current_phase"] == "captured"
    assert row["error"] == "recapture_required"
    assert row["verified_at"] is None
    assert len(events) == 1
    persisted = json.dumps(row, sort_keys=True) + json.dumps(
        [event.to_dict() for event in events], sort_keys=True
    )
    assert secret not in persisted
    assert json.loads(row["evidence_json"])["raw_sha256"] == hashlib.sha256(
        proof.canonical_raw_bytes(evidence)
    ).hexdigest()


def _advance_to_remote(proof, ledger, *, ensure_candidate: bool = True):
    previous = None
    for number, phase in enumerate(
        (
            "candidate_ready",
            "integrated_proven",
            "tests_verified",
            "review_verified",
            "artifact_frozen",
            "main_fast_forwarded",
            "remote_verified",
        ),
        start=1,
    ):
        previous = _append(
            ledger,
            operation=number,
            expected_seq=0 if previous is None else previous.event_seq,
            expected_hash=None if previous is None else previous.event_hash,
            kind=phase,
            phase=phase,
            integration_oid=None if phase == "candidate_ready" else "a" * 40,
            artifact_digest=(
                "b" * 64
                if phase
                in {"artifact_frozen", "main_fast_forwarded", "remote_verified"}
                else None
            ),
            created_at_ns=number * 1_000_000_000,
            ensure_candidate=ensure_candidate,
        )
    return previous


def test_late_async_advisory_cannot_regress_a_later_promotion_projection(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "state.db")
    _insert_v2_plan(store)
    ledger = proof.ProofLedger(store)
    first = _advance_to_remote(proof, ledger)

    assert store.mark_completed_unverified("bp_proof", {"status": "completed"}) is True
    row = store.get_plan("bp_proof")
    events = ledger.read_events("bp_proof")
    assert row["current_phase"] == "remote_verified"
    assert row["state"] == PlanState.RUNNING
    assert row["proof_event_seq"] == first.event_seq
    assert events[-1].kind == "async_terminal_advisory"
    assert events[-1].phase == "remote_verified"
    assert events[-1].stream == "advisory"


@pytest.mark.parametrize(
    ("method_name", "expected_kind", "expected_dispatch_state", "expected_error"),
    (
        ("record_dispatch_unknown", "dispatch_unknown_advisory", "unknown", "dispatch_unknown"),
        ("record_dispatch_failure", "dispatch_failed_advisory", "terminal", "dispatch_failed"),
        ("record_dispatch_deferred", "dispatch_deferred_advisory", "intent", "dispatch_deferred"),
    ),
)
def test_protocol2_dispatch_error_sinks_are_redacted_advisories(
    tmp_path,
    caplog,
    method_name,
    expected_kind,
    expected_dispatch_state,
    expected_error,
):
    proof = _proof()
    store = _store(tmp_path / f"{method_name}.db")
    _insert_v2_plan(store, state=PlanState.PENDING)
    _prepare_dispatching(store)
    secret = f"{method_name}-raw-secret"

    assert getattr(store, method_name)("bp_proof", secret) is True
    row = store.get_plan("bp_proof")
    events = proof.ProofLedger(store).read_events("bp_proof")

    assert row["state"] == PlanState.RUNNING
    assert row["current_phase"] == "captured"
    assert row["dispatch_state"] == expected_dispatch_state
    assert row["error"] == expected_error
    assert len(events) == 1
    assert events[0].kind == expected_kind
    assert events[0].stream == "advisory"
    assert events[0].raw_output_sha256 == hashlib.sha256(
        proof.canonical_raw_bytes({"status": expected_error, "detail": secret})
    ).hexdigest()
    all_sinks = json.dumps(row, sort_keys=True) + json.dumps(
        [event.to_dict() for event in events], sort_keys=True
    ) + caplog.text
    assert secret not in all_sinks


def test_protocol2_dispatch_success_does_not_persist_raw_broker_fields(tmp_path, caplog):
    proof = _proof()
    store = _store(tmp_path / "dispatch.db")
    _insert_v2_plan(store, state=PlanState.PENDING)
    _prepare_dispatching(store)
    secret = "broker-returned-sensitive-identifier"
    raw_dispatch = {
        "delegation_ids": [secret],
        "sandbox_workspace": f"/tmp/{secret}",
    }

    assert store.record_dispatch(
        "bp_proof",
        delegation_ids=raw_dispatch["delegation_ids"],
        sandbox_workspace=raw_dispatch["sandbox_workspace"],
    ) is True
    row = store.get_plan("bp_proof")
    events = proof.ProofLedger(store).read_events("bp_proof")

    assert row["state"] == PlanState.RUNNING
    assert row["current_phase"] == "captured"
    assert row["dispatch_state"] == "scheduled"
    assert json.loads(row["delegation_ids_json"]) == ["bestplan-bp_proof"]
    assert not row["sandbox_workspace"]
    assert events[-1].kind == "dispatch_scheduled_advisory"
    assert events[-1].raw_output_sha256 == hashlib.sha256(
        proof.canonical_raw_bytes(raw_dispatch)
    ).hexdigest()
    all_sinks = json.dumps(row, sort_keys=True) + json.dumps(
        [event.to_dict() for event in events], sort_keys=True
    ) + caplog.text
    assert secret not in all_sinks


def test_advisory_cannot_project_a_caller_controlled_delegation_identity(
    tmp_path, caplog
):
    proof = _proof()
    store = _store(tmp_path / "advisory-delegation-identity.db")
    _insert_v2_plan(store, state=PlanState.PENDING)
    _prepare_dispatching(store)
    before = store.get_plan("bp_proof")
    secret = "broker-controlled-delegation-sentinel"

    with pytest.raises(proof.ProofValidationError):
        proof.ProofLedger(store).append_advisory(
            plan_id="bp_proof",
            operation_id=_operation(909),
            kind="dispatch_scheduled_advisory",
            raw_output={"delegation_id": secret},
            output_source="model-broker",
            compatibility_dispatch_state="scheduled",
            compatibility_delegation_ids_json=json.dumps([secret]),
        )

    assert store.get_plan("bp_proof") == before
    assert proof.ProofLedger(store).read_events("bp_proof") == []
    sinks = json.dumps(store.get_plan("bp_proof"), sort_keys=True) + caplog.text
    assert secret not in sinks


def test_advisory_cannot_project_a_caller_controlled_error_code(tmp_path, caplog):
    proof = _proof()
    store = _store(tmp_path / "advisory-error-code.db")
    _insert_v2_plan(store, state=PlanState.PENDING)
    _prepare_dispatching(store)
    before = store.get_plan("bp_proof")
    secret = "token_shaped_secret_sentinel"

    with pytest.raises(proof.ProofValidationError):
        proof.ProofLedger(store).append_advisory(
            plan_id="bp_proof",
            operation_id=_operation(910),
            kind="dispatch_failed_advisory",
            raw_output={"error": secret},
            output_source="model-broker",
            compatibility_error=secret,
            compatibility_dispatch_state="terminal",
        )

    assert store.get_plan("bp_proof") == before
    assert proof.ProofLedger(store).read_events("bp_proof") == []
    sinks = json.dumps(store.get_plan("bp_proof"), sort_keys=True) + caplog.text
    assert secret not in sinks


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("dispatch_id", "attacker-controlled-dispatch-id"),
        (
            "resolved_runtime_json",
            '[{"lane":"attacker-controlled-runtime-sentinel"}]',
        ),
    ),
)
def test_protocol2_dispatch_identity_and_runtime_projection_are_sql_immutable(
    tmp_path, column, value
):
    store = _store(tmp_path / f"dispatch-immutable-{column}.db")
    row = _insert_v2_plan(store, state=PlanState.PENDING)
    prepared = store.prepare_dispatch_intent(
        "bp_proof",
        row["baseline_fingerprint"],
        resolved_runtimes=[],
        session_id="session",
        profile="coder",
        workspace=row["workspace"],
    )
    assert prepared is not None
    assert prepared["dispatch_id"] == "bestplan-bp_proof"
    before = store.get_plan("bp_proof")

    with pytest.raises(sqlite3.IntegrityError):
        store._execute_write(
            lambda conn: conn.execute(
                f"UPDATE bestplan_plans SET {column}=? WHERE plan_id='bp_proof'",
                (value,),
            )
        )

    assert store.get_plan("bp_proof") == before


@pytest.mark.parametrize("projection", ("dispatching", "advisory"))
def test_protocol2_dispatch_guard_rejects_composite_identity_runtime_tamper(
    tmp_path, projection
):
    store = _store(tmp_path / f"dispatch-composite-{projection}.db")
    row = _insert_v2_plan(store, state=PlanState.PENDING)
    assert store.prepare_dispatch_intent(
        "bp_proof",
        row["baseline_fingerprint"],
        resolved_runtimes=[],
        session_id="session",
        profile="coder",
        workspace=row["workspace"],
    ) is not None
    if projection == "advisory":
        assert store.record_dispatch_unknown("bp_proof", "safe-detail") is True
    before = store.get_plan("bp_proof")

    statement = (
        "UPDATE bestplan_plans SET dispatch_state='dispatching', "
        "dispatch_owner='pid:123', dispatch_id='forged-dispatch', "
        "resolved_runtime_json='[{\"route\":\"forged\"}]' "
        "WHERE plan_id='bp_proof'"
        if projection == "dispatching"
        else (
            "UPDATE bestplan_plans SET dispatch_id='forged-dispatch', "
            "resolved_runtime_json='[{\"route\":\"forged\"}]' "
            "WHERE plan_id='bp_proof'"
        )
    )
    with pytest.raises(sqlite3.IntegrityError):
        store._execute_write(lambda conn: conn.execute(statement))

    assert store.get_plan("bp_proof") == before


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("session_id", "different-session"),
        ("profile", "different-profile"),
    ),
)
def test_protocol2_execution_context_identity_is_sql_immutable(
    tmp_path, column, value
):
    store = _store(tmp_path / f"context-immutable-{column}.db")
    _insert_v2_plan(store, state=PlanState.PENDING)
    before = store.get_plan("bp_proof")

    with pytest.raises(sqlite3.IntegrityError):
        store._execute_write(
            lambda conn: conn.execute(
                f"UPDATE bestplan_plans SET {column}=? WHERE plan_id='bp_proof'",
                (value,),
            )
        )

    assert store.get_plan("bp_proof") == before


def test_v2_go_success_returns_only_host_owned_dispatch_identity(
    tmp_path, monkeypatch, caplog
):
    proof = _proof()
    store = _store(tmp_path / "go-success.db")
    row = _insert_v2_plan(store, state=PlanState.PENDING)
    secret = "opaque-broker-delegation-identity"
    go_context = _task5_go_context(tmp_path, monkeypatch)
    monkeypatch.setattr("gateway.session_context.async_delivery_supported", lambda: True)

    result = try_resolve_go(
        "go",
        session_id="session",
        workspace=row["workspace"],
        profile="coder",
        baseline_fingerprint=row["baseline_fingerprint"],
        parent_agent=object(),
        config={"autonomy": {"go_enabled": True}},
        store=store,
        runtime_resolver=lambda *_: [_v2_runtime()],
        strict_dispatcher=lambda **_: {
            "status": "dispatched",
            "delegation_id": secret,
            "sandbox_workspace": f"/tmp/{secret}",
        },
        **go_context,
    )

    assert result.status == "waiting"
    assert result.delegation_id == "bestplan-bp_proof"
    sinks = (
        json.dumps(result.to_dict(), sort_keys=True)
        + json.dumps(store.get_plan("bp_proof"), sort_keys=True)
        + json.dumps(
            [event.to_dict() for event in proof.ProofLedger(store).read_events("bp_proof")],
            sort_keys=True,
        )
        + caplog.text
    )
    assert secret not in sinks


def test_v2_go_persists_only_allowlisted_runtime_identity(
    tmp_path, monkeypatch, caplog
):
    proof = _proof()
    store = _store(tmp_path / "go-runtime-projection.db")
    row = _insert_v2_plan(store, state=PlanState.PENDING)
    secret = "unrecognized-runtime-secret-sentinel"
    dispatch_inputs = []
    go_context = _task5_go_context(tmp_path, monkeypatch)
    monkeypatch.setattr("gateway.session_context.async_delivery_supported", lambda: True)

    def dispatch(**kwargs):
        dispatch_inputs.append(kwargs["resolved_runtimes"])
        return {
            "status": "dispatched",
            "delegation_id": kwargs["dispatch_id"],
        }

    result = try_resolve_go(
        "go",
        session_id="session",
        workspace=row["workspace"],
        profile="coder",
        baseline_fingerprint=row["baseline_fingerprint"],
        parent_agent=object(),
        config={"autonomy": {"go_enabled": True}},
        store=store,
        runtime_resolver=lambda *_: [
            {
                "route": "code_worker",
                "provider": "test",
                "model": "coder",
                "runtime_fingerprint": "f" * 64,
                "opaque": secret,
                "nested_unknown": {"value": secret},
                "unknown_nonfinite": float("nan"),
            }
        ],
        strict_dispatcher=dispatch,
        **go_context,
    )

    assert result.status == "waiting"
    persisted = json.loads(store.get_plan("bp_proof")["resolved_runtime_json"])
    from tools import delegate_tool

    assert persisted == [
        {
            "model": "coder",
            "provider": "test",
            "route": "code_worker",
            "runtime_fingerprint": "f" * 64,
            "toolsets": ["file"],
            "bestplan_toolsets": ["file"],
            **delegate_tool._bestplan_host_runtime_projection(
                go_context["candidate_host_runtime"]
            ),
        }
    ]
    sinks = (
        json.dumps(result.to_dict(), sort_keys=True)
        + json.dumps(store.get_plan("bp_proof"), sort_keys=True)
        + json.dumps(store.list_for_session("session"), sort_keys=True)
        + json.dumps(
            [event.to_dict() for event in proof.ProofLedger(store).read_events("bp_proof")],
            sort_keys=True,
        )
        + json.dumps(dispatch_inputs, sort_keys=True)
        + caplog.text
    )
    assert secret not in sinks
    assert "NaN" not in store.get_plan("bp_proof")["resolved_runtime_json"]


@pytest.mark.parametrize(
    "invalid_runtime",
    (
        {"route": "code_worker", "provider": "test", "model": {"bad": "value"}},
        {"route": "code_worker", "provider": "test", "model": "coder", "runtime_fingerprint": float("inf")},
    ),
)
def test_v2_go_rejects_unsupported_allowlisted_runtime_identity(
    tmp_path, monkeypatch, invalid_runtime
):
    store = _store(tmp_path / "go-invalid-runtime.db")
    row = _insert_v2_plan(store, state=PlanState.PENDING)
    go_context = _task5_go_context(tmp_path, monkeypatch)
    monkeypatch.setattr("gateway.session_context.async_delivery_supported", lambda: True)

    result = try_resolve_go(
        "go",
        session_id="session",
        workspace=row["workspace"],
        profile="coder",
        baseline_fingerprint=row["baseline_fingerprint"],
        parent_agent=object(),
        config={"autonomy": {"go_enabled": True}},
        store=store,
        runtime_resolver=lambda *_: [invalid_runtime],
        **go_context,
    )

    assert result.status == "lane_unavailable"
    assert result.reason == "lane_unavailable"
    assert store.get_plan("bp_proof")["resolved_runtime_json"] is None


def test_v2_dispatch_projection_requires_same_transaction_advisory(tmp_path, monkeypatch):
    proof = _proof()
    store = _store(tmp_path / "dispatch-guard.db")
    row = _insert_v2_plan(store, state=PlanState.PENDING)
    claimed = store.prepare_dispatch_intent(
        "bp_proof",
        row["baseline_fingerprint"],
        resolved_runtimes=[],
        session_id="session",
        profile="coder",
        workspace=row["workspace"],
    )
    assert claimed is not None
    assert store.begin_dispatch_attempt("bp_proof") is True
    with pytest.raises(sqlite3.IntegrityError):
        store._execute_write(
            lambda conn: conn.execute(
                "UPDATE bestplan_plans SET state='waiting', "
                "delegation_ids_json='[\"raw-id\"]', sandbox_workspace='raw-work', "
                "error='raw-error' WHERE plan_id='bp_proof'"
            )
        )
    assert proof.ProofLedger(store).read_events("bp_proof") == []

    monkeypatch.setattr("agent.bestplan_state.os.kill", lambda *_: (_ for _ in ()).throw(ProcessLookupError()))
    assert store.recover_dead_dispatch_owners() == 1
    row = store.get_plan("bp_proof")
    events = proof.ProofLedger(store).read_events("bp_proof")
    assert row["state"] == PlanState.RUNNING
    assert row["current_phase"] == "captured"
    assert row["dispatch_state"] == "unknown"
    assert row["error"] == "recovered_dead_dispatch_owner"
    assert events[-1].kind == "dispatch_owner_recovered_advisory"
    assert events[-1].stream == "advisory"


@pytest.mark.parametrize(
    ("dispatch_result", "expected_status", "expected_code"),
    (
        ({"status": "rejected", "error": "opaque-broker-secret"}, "dispatch_deferred", "dispatch_deferred"),
        ({"status": "failed", "error": "opaque-broker-secret"}, "dispatch_failed", "dispatch_failed"),
        (RuntimeError("opaque-broker-secret"), "possibly_dispatched", "dispatch_unknown"),
    ),
)
def test_v2_go_returns_only_constant_codes_for_raw_broker_failures(
    tmp_path, monkeypatch, caplog, dispatch_result, expected_status, expected_code
):
    proof = _proof()
    store = _store(tmp_path / f"{expected_status}.db")
    row = _insert_v2_plan(store, state=PlanState.PENDING)
    go_context = _task5_go_context(tmp_path, monkeypatch)
    monkeypatch.setattr("gateway.session_context.async_delivery_supported", lambda: True)

    def dispatch(**_kwargs):
        if isinstance(dispatch_result, Exception):
            raise dispatch_result
        return dispatch_result

    result = try_resolve_go(
        "go",
        session_id="session",
        workspace=row["workspace"],
        profile="coder",
        baseline_fingerprint=row["baseline_fingerprint"],
        parent_agent=object(),
        config={"autonomy": {"go_enabled": True}},
        store=store,
        runtime_resolver=lambda *_: [_v2_runtime()],
        strict_dispatcher=dispatch,
        **go_context,
    )
    assert result.status == expected_status
    assert result.reason == expected_code
    assert result.error in {None, expected_code}
    sinks = (
        json.dumps(result.to_dict(), sort_keys=True)
        + json.dumps(store.get_plan("bp_proof"), sort_keys=True)
        + json.dumps(
            [event.to_dict() for event in proof.ProofLedger(store).read_events("bp_proof")],
            sort_keys=True,
        )
        + caplog.text
    )
    assert "opaque-broker-secret" not in sinks


@pytest.mark.parametrize(
    ("failure_stage", "expected_status", "expected_code"),
    (
        ("validation", "invalid_plan", "invalid_plan"),
        ("task_conversion", "invalid_plan", "invalid_plan"),
        ("pending_runtime", "lane_unavailable", "lane_unavailable"),
        ("running_runtime", "lane_unavailable", "lane_unavailable"),
    ),
)
def test_v2_go_internal_failures_return_only_stable_codes(
    tmp_path,
    monkeypatch,
    caplog,
    failure_stage,
    expected_status,
    expected_code,
):
    proof = _proof()
    store = _store(tmp_path / f"{failure_stage}.db")
    row = _insert_v2_plan(store, state=PlanState.PENDING)
    sentinel = f"opaque-{failure_stage}-internal-value"
    go_context = _task5_go_context(tmp_path, monkeypatch)

    def fail(*_args, **_kwargs):
        raise RuntimeError(sentinel)

    if failure_stage == "validation":
        monkeypatch.setattr("agent.bestplan_state._validate_stored_plan_row", fail)
    elif failure_stage == "task_conversion":
        monkeypatch.setattr("agent.bestplan_state._plan_to_delegate_tasks", fail)
    elif failure_stage == "running_runtime":
        claimed = store.prepare_dispatch_intent(
            "bp_proof",
            row["baseline_fingerprint"],
            resolved_runtimes=[_task5_stored_runtime(go_context)],
            session_id="session",
            profile="coder",
            workspace=row["workspace"],
        )
        assert claimed is not None
    monkeypatch.setattr("gateway.session_context.async_delivery_supported", lambda: True)

    result = try_resolve_go(
        "go",
        session_id="session",
        workspace=row["workspace"],
        profile="coder",
        baseline_fingerprint=row["baseline_fingerprint"],
        parent_agent=object(),
        config={"autonomy": {"go_enabled": True}},
        store=store,
        runtime_resolver=fail,
        **go_context,
    )

    assert result.status == expected_status
    assert result.reason == expected_code
    assert result.error in {None, expected_code}
    sinks = (
        json.dumps(result.to_dict(), sort_keys=True)
        + json.dumps(store.get_plan("bp_proof"), sort_keys=True)
        + json.dumps(
            [event.to_dict() for event in proof.ProofLedger(store).read_events("bp_proof")],
            sort_keys=True,
        )
        + caplog.text
    )
    assert sentinel not in sinks


def test_v1_go_runtime_failure_retains_legacy_error_text(tmp_path, monkeypatch):
    store = _store(tmp_path / "v1-runtime.db")
    _insert_v1_plan(store, state=PlanState.APPROVED)
    row = store.get_plan("bp_legacy")
    sentinel = "legacy-runtime-error-text"
    monkeypatch.setattr("gateway.session_context.async_delivery_supported", lambda: True)

    def fail(*_args, **_kwargs):
        raise RuntimeError(sentinel)

    result = try_resolve_go(
        "go",
        session_id="session",
        workspace=row["workspace"],
        profile="coder",
        baseline_fingerprint=row["baseline_fingerprint"],
        parent_agent=object(),
        config={"autonomy": {"go_enabled": True}},
        store=store,
        runtime_resolver=fail,
        strict_dispatcher=lambda **_kwargs: pytest.fail(
            "runtime failure must happen before dispatch"
        ),
    )

    assert result.status == "lane_unavailable"
    assert result.reason == sentinel


def test_late_protocol2_dispatch_callback_cannot_regress_authority_projection(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "late-dispatch.db")
    _insert_v2_plan(store, state=PlanState.PENDING)
    _prepare_dispatching(store)
    ledger = proof.ProofLedger(store)
    authority_head = _advance_to_remote(proof, ledger)

    assert store.record_dispatch_failure(
        "bp_proof", "late-dispatch-sensitive-output"
    ) is True
    row = store.get_plan("bp_proof")
    event = ledger.read_events("bp_proof")[-1]

    assert row["state"] == PlanState.RUNNING
    assert row["current_phase"] == "remote_verified"
    assert row["proof_event_seq"] == authority_head.event_seq
    assert row["proof_event_hash"] == authority_head.event_hash
    assert event.stream == "advisory"
    assert event.phase == "remote_verified"


def _two_dispatch_advisories(proof, store):
    _insert_v2_plan(store, state=PlanState.PENDING)
    _prepare_dispatching(store)
    assert store.record_dispatch_unknown("bp_proof", "first-opaque-output") is True
    assert store.record_dispatch_failure("bp_proof", "second-opaque-output") is True
    events = [
        event
        for event in proof.ProofLedger(store).read_events("bp_proof")
        if event.stream == "advisory"
    ]
    assert len(events) == 2
    return events


def test_v2_direct_sql_cannot_replay_an_older_advisory_projection(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "advisory-replay.db")
    first, _ = _two_dispatch_advisories(proof, store)
    before = store.get_plan("bp_proof")

    with pytest.raises(sqlite3.IntegrityError):
        store._execute_write(
            lambda conn: conn.execute(
                "UPDATE bestplan_plans SET evidence_json=?, error=?, "
                "dispatch_state=? WHERE plan_id='bp_proof'",
                (
                    first.payload_json,
                    first.compatibility_error,
                    first.compatibility_dispatch_state,
                ),
            )
        )

    assert store.get_plan("bp_proof") == before


def test_verify_chain_binds_the_latest_advisory_projection(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "advisory-head.db")
    first, _ = _two_dispatch_advisories(proof, store)

    def replay(conn):
        conn.execute("DROP TRIGGER bestplan_plans_v2_dispatch_guard_v1")
        conn.execute("DROP TRIGGER bestplan_plans_v2_compatibility_guard_v1")
        conn.execute(
            "UPDATE bestplan_plans SET evidence_json=?, error=?, "
            "dispatch_state=? WHERE plan_id='bp_proof'",
            (
                first.payload_json,
                first.compatibility_error,
                first.compatibility_dispatch_state,
            ),
        )

    store._execute_write(replay)
    with pytest.raises(proof.ProofValidationError, match="advisory"):
        proof.ProofLedger(store).verify_chain("bp_proof")


def test_v2_direct_sql_cannot_replay_an_older_authority_projection(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "authority-replay.db")
    _insert_v2_plan(store)
    ledger = proof.ProofLedger(store)
    first = _append(ledger)
    _append(
        ledger,
        operation=2,
        expected_seq=first.event_seq,
        expected_hash=first.event_hash,
        kind="integrated_proven",
        phase="integrated_proven",
        integration_oid="a" * 40,
        raw_output={"status": "second-authority-output"},
        created_at_ns=2_000_000_000,
    )
    before = store.get_plan("bp_proof")

    with pytest.raises(sqlite3.IntegrityError):
        store._execute_write(
            lambda conn: conn.execute(
                "UPDATE bestplan_plans SET evidence_json=? WHERE plan_id='bp_proof'",
                (first.payload_json,),
            )
        )

    assert store.get_plan("bp_proof") == before


def test_verify_chain_binds_authority_evidence_to_current_event_pointer(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "authority-head.db")
    _insert_v2_plan(store)
    ledger = proof.ProofLedger(store)
    first = _append(ledger, raw_output={"status": "first-authority-output"})
    _append(
        ledger,
        operation=2,
        expected_seq=first.event_seq,
        expected_hash=first.event_hash,
        kind="integrated_proven",
        phase="integrated_proven",
        integration_oid="a" * 40,
        raw_output={"status": "second-authority-output"},
        created_at_ns=2_000_000_000,
    )

    def replay(conn):
        conn.execute("DROP TRIGGER bestplan_plans_v2_compatibility_guard_v1")
        conn.execute(
            "UPDATE bestplan_plans SET evidence_json=? WHERE plan_id='bp_proof'",
            (first.payload_json,),
        )

    store._execute_write(replay)
    with pytest.raises(proof.ProofValidationError, match="authority"):
        ledger.verify_chain("bp_proof")


def test_verify_chain_revalidates_the_stored_plan_contract_in_its_snapshot(
    tmp_path,
):
    proof = _proof()
    db_path = tmp_path / "stored-plan-contract.db"
    store = _store(db_path)
    _insert_v2_plan(store)
    assert _append(proof.ProofLedger(store)).phase == "candidate_ready"

    def corrupt(conn):
        conn.execute("DROP TRIGGER bestplan_plans_v2_immutable_inputs_v1")
        conn.execute(
            "UPDATE bestplan_plans SET promotion_contract_json='{}' "
            "WHERE plan_id='bp_proof'"
        )

    store._execute_write(corrupt)
    store.close()
    reopened = _store(db_path)

    with pytest.raises(proof.ProofValidationError):
        proof.ProofLedger(reopened).verify_chain("bp_proof")


def test_protocol2_startup_reconcile_terminal_output_is_only_advisory(tmp_path, caplog):
    proof = _proof()
    store = _store(tmp_path / "state.db")
    _insert_v2_plan(store)
    secret = "tracker-terminal-raw-secret"
    delegation_id = "bestplan-bp_proof"
    terminal_event = {
        "delegation_id": delegation_id,
        "status": "completed",
        "result": {"authorization": f"Bearer {secret}"},
    }
    (tmp_path / "async_delegations.json").write_text(
        json.dumps(
            {
                "version": 1,
                "records": {
                    delegation_id: {
                        "status": "completed",
                        "event": terminal_event,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert store.reconcile_async_tracker() == 1
    row = store.get_plan("bp_proof")
    events = proof.ProofLedger(store).read_events("bp_proof")

    assert row["state"] == PlanState.RUNNING
    assert row["current_phase"] == "captured"
    assert row["dispatch_state"] == "terminal"
    assert row["error"] == "recapture_required"
    assert events[-1].kind == "async_tracker_terminal_advisory"
    assert events[-1].raw_output_sha256 == hashlib.sha256(
        proof.canonical_raw_bytes(terminal_event)
    ).hexdigest()
    all_sinks = json.dumps(row, sort_keys=True) + json.dumps(
        [event.to_dict() for event in events], sort_keys=True
    ) + caplog.text
    assert secret not in all_sinks


@pytest.mark.parametrize(
    "tracker_entry",
    (
        {
            "status": "completed",
            "event": {
                "status": "completed",
                "timestamp": 1_723_456_789.25,
                "duration_seconds": 0.75,
                "message": "tracker-opaque-sentinel",
            },
        },
        {
            "status": "completed",
            "result": [
                {
                    "duration_seconds": 0.5,
                    "message": "tracker-opaque-sentinel",
                }
            ],
        },
    ),
)
def test_v2_startup_reconcile_falls_back_safely_for_unredactable_tracker_payload(
    tmp_path, tracker_entry, caplog
):
    proof = _proof()
    db_path = tmp_path / "tracker-fallback.db"
    seed = _store(db_path)
    _insert_v2_plan(seed)
    seed.close()
    (tmp_path / "async_delegations.json").write_text(
        json.dumps(
            {
                "version": 1,
                "records": {"bestplan-bp_proof": tracker_entry},
            }
        ),
        encoding="utf-8",
    )

    reopened = _store(db_path)
    row = reopened.get_plan("bp_proof")
    events = proof.ProofLedger(reopened).read_events("bp_proof")
    sinks = (
        json.dumps(row, sort_keys=True)
        + json.dumps([event.to_dict() for event in events], sort_keys=True)
        + caplog.text
    )

    assert row["dispatch_state"] == "terminal"
    assert row["error"] == "recapture_required"
    assert events[-1].kind == "async_tracker_terminal_advisory"
    assert "tracker-opaque-sentinel" not in sinks


def _authority_event(
    proof,
    store,
    *,
    origin: str = "authority",
    ensure_candidate: bool = True,
):
    ledger = proof.ProofLedger(store)
    if ensure_candidate:
        _candidate(ledger)
    previous = _advance_to_remote(
        proof, ledger, ensure_candidate=ensure_candidate
    )
    row = store.get_plan("bp_proof")
    event = _append(
        ledger,
        operation=8,
        expected_seq=previous.event_seq,
        expected_hash=previous.event_hash,
        kind="live_verified",
        phase="live_verified",
        state=PlanState.COMPLETED_UNVERIFIED,
        origin=origin,
        contract_digest_value=row["promotion_contract_digest"],
        integration_oid="a" * 40,
        artifact_digest="b" * 64,
        raw_output={"status": "live", "target": "gateway-primary"},
        created_at_ns=12_000_000_000,
    )
    verification = proof.make_authority_verification(
        plan_row=store.get_plan("bp_proof"),
        event=event,
        observed_local_oid="a" * 40,
        observed_remote_oid="a" * 40,
        observed_live_release="a" * 40,
        observed_live_artifact_digest="b" * 64,
        issued_at_ns=10_000_000_000,
        expires_at_ns=20_000_000_000,
    )
    return verification, event


def _candidate(ledger):
    return ledger.record_candidate(
        plan_id="bp_proof",
        candidate_id="candidate-1",
        slice_id="code",
        attempt_id="attempt-1",
        commit_oid="c" * 40,
        tree_oid="d" * 40,
        raw_receipt={"status": "frozen"},
        created_at_ns=1,
    )


def test_complete_verified_requires_fresh_external_verification_and_exact_bindings(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "state.db")
    _insert_v2_plan(store)
    ledger = proof.ProofLedger(store)
    _candidate(ledger)
    verification, event = _authority_event(proof, store)
    calls = []

    def verifier(received, plan_row):
        calls.append((received, plan_row["plan_id"]))
        return True

    assert store.get_plan("bp_proof")["live_verified_at"] == 12.0
    assert store.get_plan("bp_proof")["verified_at"] is None
    candidate_set_digest = store.get_plan("bp_proof")["candidate_set_digest"]
    assert candidate_set_digest
    assert all(
        item.candidate_set_digest == candidate_set_digest
        for item in ledger.read_events("bp_proof")
        if item.stream == "authority"
    )
    assert json.loads(verification.receipt_json)["candidate_set_digest"] == candidate_set_digest
    assert ledger.complete_verified(
        "bp_proof",
        verification,
        verifier=verifier,
        now_ns=13_000_000_000,
    ) is True

    row = store.get_plan("bp_proof")
    assert calls == [(verification, "bp_proof")]
    assert row["state"] == PlanState.COMPLETED_VERIFIED
    assert row["verified_at"] == 13.0
    assert row["verification_receipt_digest"] == verification.receipt_digest
    assert row["proof_event_seq"] == event.event_seq
    assert row["proof_event_hash"] == event.event_hash


def test_authority_verification_rejects_extra_top_level_plaintext(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "authority-extra-key.db")
    _insert_v2_plan(store)
    verification, _ = _authority_event(proof, store)
    body = json.loads(verification.receipt_json)
    body["raw_text"] = "authority-raw-plaintext-sentinel"
    receipt_json = proof._canonical_json(body)

    with pytest.raises(proof.ProofValidationError):
        proof.AuthorityVerification(
            **{
                **verification.to_dict(),
                "receipt_json": receipt_json,
                "receipt_digest": proof._digest(
                    receipt_json, proof.AUTHORITY_RECEIPT_DOMAIN
                ),
            }
        )

    row = store.get_plan("bp_proof")
    assert row["verification_receipt_json"] is None
    assert row["verification_receipt_digest"] is None
    assert store._connection().execute(
        "SELECT COUNT(*) FROM bestplan_verification_receipts "
        "WHERE plan_id='bp_proof'"
    ).fetchone()[0] == 0


def test_authority_append_rejects_gateway_origin_before_projection(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "gateway-authority-origin.db")
    _insert_v2_plan(store)

    with pytest.raises(proof.ProofValidationError, match="origin"):
        _append(proof.ProofLedger(store), origin="gateway")

    assert store.get_plan("bp_proof")["current_phase"] == "captured"


@pytest.mark.parametrize("completed_first", (False, True))
def test_completion_and_retry_validate_the_full_authority_chain(
    tmp_path, completed_first
):
    proof = _proof()
    store = _store(tmp_path / f"full-chain-{completed_first}.db")
    _insert_v2_plan(store)
    verification, _ = _authority_event(proof, store)
    ledger = proof.ProofLedger(store)
    if completed_first:
        assert ledger.complete_verified(
            "bp_proof",
            verification,
            verifier=lambda *_: True,
            now_ns=13_000_000_000,
        ) is True

    def corrupt(conn):
        conn.execute("DROP TRIGGER bestplan_proof_events_no_update_v1")
        conn.execute(
            "UPDATE bestplan_proof_events SET origin='gateway' "
            "WHERE plan_id='bp_proof' AND stream='authority' AND event_seq=1"
        )

    store._execute_write(corrupt)
    verifier_calls = []
    with pytest.raises(proof.ProofValidationError):
        ledger.complete_verified(
            "bp_proof",
            verification,
            verifier=lambda *_: verifier_calls.append(True) or True,
            now_ns=21_000_000_000 if completed_first else 13_000_000_000,
        )
    assert verifier_calls == []


def test_completed_verified_retry_is_pointer_stable_after_receipt_expiry(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "completed-retry.db")
    _insert_v2_plan(store)
    verification, _ = _authority_event(proof, store)
    ledger = proof.ProofLedger(store)
    assert ledger.complete_verified(
        "bp_proof",
        verification,
        verifier=lambda *_: True,
        now_ns=13_000_000_000,
    ) is True
    witness = store._connection().execute(
        "SELECT * FROM bestplan_verification_receipts WHERE plan_id='bp_proof'"
    ).fetchone()
    assert witness is not None
    assert witness["receipt_json"] == verification.receipt_json
    assert witness["receipt_digest"] == verification.receipt_digest
    assert witness["event_hash"] == verification.event_hash

    verifier_calls = []

    def verifier_must_not_run(*_args):
        verifier_calls.append(True)
        raise AssertionError("completed retry must not re-authenticate")

    assert ledger.complete_verified(
        "bp_proof",
        verification,
        verifier=verifier_must_not_run,
        now_ns=21_000_000_000,
    ) is True
    assert verifier_calls == []
    assert store._connection().execute(
        "SELECT COUNT(*) FROM bestplan_verification_receipts "
        "WHERE plan_id='bp_proof'"
    ).fetchone()[0] == 1

    for statement in (
        "UPDATE bestplan_verification_receipts SET receipt_digest='" + "f" * 64
        + "' WHERE plan_id='bp_proof'",
        "DELETE FROM bestplan_verification_receipts WHERE plan_id='bp_proof'",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            store._execute_write(lambda conn, sql=statement: conn.execute(sql))

    body = json.loads(verification.receipt_json)
    body["remote"]["ref"] = "refs/heads/different"
    different_json = json.dumps(
        body,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    different = proof.AuthorityVerification(
        **{
            **verification.to_dict(),
            "receipt_json": different_json,
            "receipt_digest": hashlib.sha256(
                b"hermes.bestplan.authority-receipt.v1\0"
                + different_json.encode("ascii")
            ).hexdigest(),
        }
    )
    with pytest.raises(proof.ProofValidationError, match="different receipt"):
        ledger.complete_verified(
            "bp_proof",
            different,
            verifier=verifier_must_not_run,
            now_ns=21_000_000_000,
        )
    assert verifier_calls == []


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("verification_receipt_json", "{}"),
        ("verification_receipt_digest", "f" * 64),
        ("verified_at", 14.0),
        ("current_phase", "captured"),
        ("integration_oid", "f" * 40),
        ("artifact_digest", "f" * 64),
        ("proof_authority_epoch", "other-epoch"),
        ("proof_event_seq", 999),
        ("proof_event_hash", "f" * 64),
        ("candidate_set_digest", "f" * 64),
        ("tests_verified_at", 99.0),
        ("review_verified_at", 99.0),
        ("remote_verified_at", 99.0),
        ("live_verified_at", 99.0),
        ("completed_at", 99.0),
    ),
)
def test_completed_v2_semantic_and_receipt_fields_are_sql_immutable(
    tmp_path, column, value
):
    proof = _proof()
    store = _store(tmp_path / f"immutable-{column}.db")
    _insert_v2_plan(store)
    verification, _ = _authority_event(proof, store)
    assert proof.ProofLedger(store).complete_verified(
        "bp_proof",
        verification,
        verifier=lambda *_: True,
        now_ns=13_000_000_000,
    ) is True
    before = store.get_plan("bp_proof")

    with pytest.raises(sqlite3.IntegrityError):
        store._execute_write(
            lambda conn: conn.execute(
                f"UPDATE bestplan_plans SET {column}=? WHERE plan_id=?",
                (value, "bp_proof"),
            )
        )

    assert store.get_plan("bp_proof") == before


@pytest.mark.parametrize(
    "corruption",
    ("receipt", "contract", "authority_event", "candidate_set", "timestamps"),
)
def test_completed_retry_revalidates_stored_proof_before_idempotent_return(
    tmp_path, corruption
):
    proof = _proof()
    store = _store(tmp_path / f"completed-corrupt-{corruption}.db")
    _insert_v2_plan(store)
    verification, _ = _authority_event(proof, store)
    ledger = proof.ProofLedger(store)
    assert ledger.complete_verified(
        "bp_proof",
        verification,
        verifier=lambda *_: True,
        now_ns=13_000_000_000,
    ) is True
    completed_row = store.get_plan("bp_proof")

    def corrupt(conn):
        if corruption == "receipt":
            conn.execute("DROP TRIGGER bestplan_plans_v2_receipt_guard_v1")
            noncanonical = verification.receipt_json + " "
            digest = hashlib.sha256(
                b"hermes.bestplan.authority-receipt.v1\0"
                + noncanonical.encode("ascii")
            ).hexdigest()
            object.__setattr__(verification, "receipt_json", noncanonical)
            object.__setattr__(verification, "receipt_digest", digest)
            conn.execute(
                "UPDATE bestplan_plans SET verification_receipt_json=?, "
                "verification_receipt_digest=? WHERE plan_id='bp_proof'",
                (noncanonical, digest),
            )
        elif corruption == "contract":
            conn.execute("DROP TRIGGER bestplan_plans_v2_immutable_inputs_v1")
            body = json.loads(completed_row["promotion_contract_json"])
            body["publication"]["remote_ref"] = "refs/heads/corrupt"
            conn.execute(
                "UPDATE bestplan_plans SET promotion_contract_json=? "
                "WHERE plan_id='bp_proof'",
                (json.dumps(body, ensure_ascii=True, sort_keys=True, separators=(",", ":")),),
            )
        elif corruption == "authority_event":
            conn.execute("DROP TRIGGER bestplan_proof_events_no_delete_v1")
            conn.execute(
                "DELETE FROM bestplan_proof_events WHERE plan_id='bp_proof' "
                "AND stream='authority' AND kind='live_verified'"
            )
        elif corruption == "candidate_set":
            conn.execute("DROP TRIGGER bestplan_candidates_no_delete_v1")
            conn.execute("DELETE FROM bestplan_candidates WHERE plan_id='bp_proof'")
        else:
            conn.execute("DROP TRIGGER bestplan_plans_v2_receipt_guard_v1")
            conn.execute(
                "UPDATE bestplan_plans SET verified_at=9 WHERE plan_id='bp_proof'"
            )

    store._execute_write(corrupt)
    verifier_calls = []

    with pytest.raises(proof.ProofValidationError):
        ledger.complete_verified(
            "bp_proof",
            verification,
            verifier=lambda *_: verifier_calls.append(True) or True,
            now_ns=21_000_000_000,
        )
    assert verifier_calls == []
    with pytest.raises(proof.ProofValidationError):
        ledger.verify_chain("bp_proof")


def test_verify_chain_accepts_fully_validated_completed_terminal_overlay(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "completed-chain.db")
    _insert_v2_plan(store)
    verification, _ = _authority_event(proof, store)
    ledger = proof.ProofLedger(store)
    assert ledger.complete_verified(
        "bp_proof",
        verification,
        verifier=lambda *_: True,
        now_ns=13_000_000_000,
    ) is True

    assert ledger.verify_chain("bp_proof") is True


@pytest.mark.parametrize(
    "terminal_state",
    (PlanState.COMPLETED_UNVERIFIED, PlanState.COMPLETED_VERIFIED),
)
def test_verify_chain_rejects_terminal_v2_projection_with_no_authority_chain(
    tmp_path, terminal_state
):
    proof = _proof()
    store = _store(tmp_path / f"no-authority-{terminal_state}.db")
    _insert_v2_plan(store, state=terminal_state)

    with pytest.raises(proof.ProofValidationError, match="authority"):
        proof.ProofLedger(store).verify_chain("bp_proof")


def test_verify_chain_rejects_authority_chain_split_across_epochs(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "split-authority-epoch.db")
    _insert_v2_plan(store)
    ledger = proof.ProofLedger(store)
    first = _append(ledger)
    second = _append(
        ledger,
        operation=2,
        expected_seq=first.event_seq,
        expected_hash=first.event_hash,
        kind="integrated_proven",
        phase="integrated_proven",
        integration_oid="a" * 40,
        created_at_ns=2_000_000_000,
    )
    split = second.to_dict()
    split.update(
        {
            "authority_epoch": "epoch-4",
            "event_seq": 1,
            "previous_hash": None,
            "operation_id": _operation(99),
        }
    )
    fingerprint_values = {
        **split,
        "expected_epoch": None,
        "expected_seq": 0,
        "expected_hash": None,
    }
    split["operation_fingerprint"] = proof._operation_fingerprint(
        fingerprint_values
    )
    split["event_hash"] = proof._event_hash(split)

    def corrupt(conn):
        conn.execute("DROP TRIGGER bestplan_proof_events_no_update_v1")
        conn.execute("DROP TRIGGER bestplan_plans_v2_projection_guard_v1")
        conn.execute(
            """UPDATE bestplan_proof_events SET
                   authority_epoch=?, event_seq=?, previous_hash=?, operation_id=?,
                   operation_fingerprint=?, event_hash=?
               WHERE plan_id=? AND stream='authority' AND event_hash=?""",
            (
                split["authority_epoch"],
                split["event_seq"],
                split["previous_hash"],
                split["operation_id"],
                split["operation_fingerprint"],
                split["event_hash"],
                second.plan_id,
                second.event_hash,
            ),
        )
        conn.execute(
            "UPDATE bestplan_plans SET proof_authority_epoch=?, proof_event_seq=?, "
            "proof_event_hash=? WHERE plan_id=?",
            (
                split["authority_epoch"],
                split["event_seq"],
                split["event_hash"],
                second.plan_id,
            ),
        )

    store._execute_write(corrupt)
    with pytest.raises(proof.ProofValidationError, match="authority epoch"):
        ledger.verify_chain("bp_proof")


def test_verify_chain_recomputes_candidate_aggregate_for_nonterminal_chain(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "missing-nonterminal-candidate.db")
    _insert_v2_plan(store)
    ledger = proof.ProofLedger(store)
    _append(ledger)

    def corrupt(conn):
        conn.execute("DROP TRIGGER bestplan_candidates_no_delete_v1")
        conn.execute("DELETE FROM bestplan_candidates WHERE plan_id='bp_proof'")

    store._execute_write(corrupt)
    with pytest.raises(proof.ProofValidationError, match="candidate"):
        ledger.verify_chain("bp_proof")


@pytest.mark.parametrize("corruption", ("bindings", "kind_origin", "state"))
def test_verify_chain_rejects_self_hashed_authority_semantic_corruption(
    tmp_path, corruption
):
    proof = _proof()
    store = _store(tmp_path / f"authority-semantic-{corruption}.db")
    _insert_v2_plan(store)
    ledger = proof.ProofLedger(store)
    event = _append(ledger)
    changed = event.to_dict()
    if corruption == "bindings":
        changed.update(
            {
                "approval_digest": "f" * 64,
                "contract_digest": "e" * 64,
                "source_snapshot_digest": "d" * 64,
                "base_oid": "c" * 40,
            }
        )
    elif corruption == "kind_origin":
        changed.update({"kind": "forged_kind", "origin": "gateway"})
    else:
        changed["projected_state"] = "failed"
    fingerprint_values = {
        **changed,
        "expected_epoch": None,
        "expected_seq": 0,
        "expected_hash": None,
    }
    changed["operation_fingerprint"] = proof._operation_fingerprint(
        fingerprint_values
    )
    changed["event_hash"] = proof._event_hash(changed)

    def corrupt(conn):
        conn.execute("DROP TRIGGER bestplan_proof_events_no_update_v1")
        conn.execute("DROP TRIGGER bestplan_plans_v2_projection_guard_v1")
        conn.execute(
            """UPDATE bestplan_proof_events SET approval_digest=?,
                   contract_digest=?, source_snapshot_digest=?, base_oid=?, kind=?,
                   origin=?, projected_state=?, operation_fingerprint=?, event_hash=?
               WHERE plan_id=? AND stream='authority'""",
            (
                changed["approval_digest"],
                changed["contract_digest"],
                changed["source_snapshot_digest"],
                changed["base_oid"],
                changed["kind"],
                changed["origin"],
                changed["projected_state"],
                changed["operation_fingerprint"],
                changed["event_hash"],
                event.plan_id,
            ),
        )
        conn.execute(
            "UPDATE bestplan_plans SET state=?, proof_event_hash=? WHERE plan_id=?",
            (
                changed["projected_state"],
                changed["event_hash"],
                event.plan_id,
            ),
        )

    store._execute_write(corrupt)
    with pytest.raises(proof.ProofValidationError):
        ledger.verify_chain("bp_proof")


def test_verify_chain_reads_events_and_projection_from_one_sqlite_snapshot(
    tmp_path, monkeypatch
):
    proof = _proof()
    db_path = tmp_path / "chain-snapshot.db"
    writer_store = _store(db_path)
    _insert_v2_plan(writer_store)
    writer_ledger = proof.ProofLedger(writer_store)
    first = _append(writer_ledger)
    reader_store = _store(db_path)
    reader_ledger = proof.ProofLedger(reader_store)
    first_event_read = threading.Event()
    writer_done = threading.Event()
    writer_errors = []
    original_validate = reader_ledger._validate_event_receipt

    def validate_then_pause(event):
        original_validate(event)
        if event.stream == "authority" and event.event_seq == 1:
            first_event_read.set()
            assert writer_done.wait(timeout=5)

    monkeypatch.setattr(reader_ledger, "_validate_event_receipt", validate_then_pause)

    def append_concurrently():
        try:
            assert first_event_read.wait(timeout=5)
            _append(
                writer_ledger,
                operation=2,
                expected_seq=first.event_seq,
                expected_hash=first.event_hash,
                kind="integrated_proven",
                phase="integrated_proven",
                integration_oid="a" * 40,
                created_at_ns=2_000_000_000,
            )
        except BaseException as exc:
            writer_errors.append(exc)
        finally:
            writer_done.set()

    thread = threading.Thread(target=append_concurrently)
    thread.start()
    try:
        assert reader_ledger.verify_chain("bp_proof") is True
    finally:
        writer_done.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert writer_errors == []
    assert writer_store.get_plan("bp_proof")["current_phase"] == "integrated_proven"


def test_complete_rechecks_full_receipt_contract_and_expiry_after_verifier(tmp_path):
    proof = _proof()

    contract_store = _store(tmp_path / "receipt-contract.db")
    _insert_v2_plan(contract_store)
    verification, _ = _authority_event(proof, contract_store)
    body = json.loads(verification.receipt_json)
    body["remote"]["ref"] = "refs/heads/other"
    mutated_json = json.dumps(body, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    mutated = proof.AuthorityVerification(
        **{
            **verification.to_dict(),
            "receipt_json": mutated_json,
            "receipt_digest": hashlib.sha256(
                b"hermes.bestplan.authority-receipt.v1\0" + mutated_json.encode("ascii")
            ).hexdigest(),
        }
    )
    verifier_calls = []
    with pytest.raises(proof.ProofValidationError, match="receipt.*contract|contract.*receipt"):
        proof.ProofLedger(contract_store).complete_verified(
            "bp_proof",
            mutated,
            verifier=lambda *_: verifier_calls.append(True) or True,
            now_ns=13_000_000_000,
        )
    assert verifier_calls == [True]
    assert contract_store.get_plan("bp_proof")["verified_at"] is None

    expiry_store = _store(tmp_path / "receipt-expiry.db")
    _insert_v2_plan(expiry_store)
    expiring, _ = _authority_event(proof, expiry_store)
    times = iter((13_000_000_000, 21_000_000_000))
    with pytest.raises(proof.ProofValidationError, match="expired before CAS"):
        proof.ProofLedger(expiry_store).complete_verified(
            "bp_proof",
            expiring,
            verifier=lambda *_: True,
            clock_ns=lambda: next(times),
        )
    assert expiry_store.get_plan("bp_proof")["verified_at"] is None


def test_initial_completion_revalidates_mutated_verification_dto(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "mutated-terminal-dto.db")
    _insert_v2_plan(store)
    verification, _ = _authority_event(proof, store)
    object.__setattr__(verification, "receipt_digest", "f" * 64)
    verifier_calls = []

    with pytest.raises(proof.ProofValidationError, match="digest"):
        proof.ProofLedger(store).complete_verified(
            "bp_proof",
            verification,
            verifier=lambda *_: verifier_calls.append(True) or True,
            now_ns=13_000_000_000,
        )

    assert verifier_calls == []
    assert store.get_plan("bp_proof")["state"] == PlanState.COMPLETED_UNVERIFIED


def test_failed_terminal_cas_rolls_back_verification_witness(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "terminal-witness-rollback.db")
    _insert_v2_plan(store)
    verification, _ = _authority_event(proof, store)
    store._execute_write(
        lambda conn: conn.execute(
            """CREATE TRIGGER test_reject_terminal_cas
               BEFORE UPDATE ON bestplan_plans
               WHEN NEW.state='completed_verified'
               BEGIN SELECT RAISE(ABORT, 'test terminal CAS rejection'); END"""
        )
    )

    with pytest.raises(proof.ProofValidationError):
        proof.ProofLedger(store).complete_verified(
            "bp_proof",
            verification,
            verifier=lambda *_: True,
            now_ns=13_000_000_000,
        )

    assert store.get_plan("bp_proof")["state"] == PlanState.COMPLETED_UNVERIFIED
    assert store._connection().execute(
        "SELECT COUNT(*) FROM bestplan_verification_receipts "
        "WHERE plan_id='bp_proof'"
    ).fetchone()[0] == 0


def test_terminal_cas_rolls_back_nonmonotonic_completed_timestamps(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "terminal-timestamp-rollback.db")
    _insert_v2_plan(store)
    verification, _ = _authority_event(proof, store)

    def corrupt(conn):
        conn.execute("DROP TRIGGER bestplan_plans_v2_projection_guard_v1")
        conn.execute(
            "UPDATE bestplan_plans SET tests_verified_at=15 "
            "WHERE plan_id='bp_proof'"
        )

    store._execute_write(corrupt)
    with pytest.raises(proof.ProofValidationError, match="timestamp"):
        proof.ProofLedger(store).complete_verified(
            "bp_proof",
            verification,
            verifier=lambda *_: True,
            now_ns=13_000_000_000,
        )

    assert store.get_plan("bp_proof")["state"] == PlanState.COMPLETED_UNVERIFIED
    assert store._connection().execute(
        "SELECT COUNT(*) FROM bestplan_verification_receipts "
        "WHERE plan_id='bp_proof'"
    ).fetchone()[0] == 0


def test_local_chain_false_verifier_stale_receipt_and_missing_candidate_cannot_complete(tmp_path):
    proof = _proof()

    missing_store = _store(tmp_path / "missing.db")
    _insert_v2_plan(missing_store)
    with pytest.raises(proof.ProofValidationError, match="candidate receipt"):
        _authority_event(proof, missing_store, ensure_candidate=False)

    local_store = _store(tmp_path / "local.db")
    _insert_v2_plan(local_store)
    local_ledger = proof.ProofLedger(local_store)
    _candidate(local_ledger)
    with pytest.raises(proof.ProofValidationError, match="origin"):
        _authority_event(proof, local_store, origin="gateway")
    assert local_store.get_plan("bp_proof")["state"] == PlanState.RUNNING

    false_store = _store(tmp_path / "false.db")
    _insert_v2_plan(false_store)
    false_ledger = proof.ProofLedger(false_store)
    _candidate(false_ledger)
    false_verification, false_event = _authority_event(proof, false_store)
    with pytest.raises(proof.ProofValidationError, match="verifier rejected"):
        false_ledger.complete_verified(
            "bp_proof",
            false_verification,
            verifier=lambda *_: False,
            now_ns=13_000_000_000,
        )
    assert false_store.get_plan("bp_proof")["verified_at"] is None

    stale_body = json.loads(false_verification.receipt_json)
    stale_body["event_hash"] = "f" * 64
    stale_json = json.dumps(
        stale_body,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    stale = proof.AuthorityVerification(
        **{
            **false_verification.to_dict(),
            "receipt_json": stale_json,
            "receipt_digest": hashlib.sha256(
                b"hermes.bestplan.authority-receipt.v1\0" + stale_json.encode("ascii")
            ).hexdigest(),
            "event_hash": "f" * 64,
        }
    )
    with pytest.raises(proof.ProofValidationError, match="event pointer"):
        false_ledger.complete_verified(
            "bp_proof", stale, verifier=lambda *_: True, now_ns=13_000_000_000
        )


def test_even_matching_local_rows_cannot_terminal_cas_without_receipt_json(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "state.db")
    _insert_v2_plan(store)
    _candidate(proof.ProofLedger(store))
    _authority_event(proof, store)

    with pytest.raises(sqlite3.IntegrityError):
        store._execute_write(
            lambda conn: conn.execute(
                "UPDATE bestplan_plans SET state='completed_verified', "
                "verified_at=12 WHERE plan_id='bp_proof'"
            )
        )


def test_direct_sql_cannot_complete_with_fabricated_receipt_fields(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "fabricated-terminal-receipt.db")
    _insert_v2_plan(store)
    _candidate(proof.ProofLedger(store))
    _authority_event(proof, store)
    before = store.get_plan("bp_proof")
    fake_json = json.dumps(
        {
            "schema": "hermes.bestplan.authority-receipt.v1",
            "version": 1,
            "kind": "live_verified",
            "plan_id": "bp_proof",
            "fabricated": True,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    fake_digest = hashlib.sha256(
        b"hermes.bestplan.authority-receipt.v1\0" + fake_json.encode("ascii")
    ).hexdigest()

    with pytest.raises(sqlite3.IntegrityError):
        store._execute_write(
            lambda conn: conn.execute(
                "UPDATE bestplan_plans SET state='completed_verified', "
                "verification_receipt_json=?, verification_receipt_digest=?, "
                "verified_at=13 WHERE plan_id='bp_proof'",
                (fake_json, fake_digest),
            )
        )

    assert store.get_plan("bp_proof") == before


_LEGACY_PLAN_SCHEMA = """
CREATE TABLE bestplan_plans (
    plan_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    session_id TEXT,
    profile TEXT NOT NULL,
    workspace TEXT NOT NULL,
    baseline_fingerprint TEXT NOT NULL,
    raw_request TEXT,
    raw_plan_json TEXT NOT NULL,
    validated_manifest_json TEXT NOT NULL,
    state TEXT NOT NULL,
    approved_at REAL,
    approved_by TEXT,
    approval_digest TEXT,
    started_at REAL,
    completed_at REAL,
    delegation_ids_json TEXT,
    evidence_json TEXT,
    error TEXT
)
"""


def test_old_and_partial_schemas_migrate_additively_without_udf_dependencies(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(_LEGACY_PLAN_SCHEMA)
    conn.execute(
        "CREATE TABLE bestplan_proof_events (plan_id TEXT, authority_epoch TEXT, event_seq INTEGER)"
    )
    conn.execute("CREATE TABLE bestplan_candidates (plan_id TEXT, candidate_id TEXT)")
    plan = _plan()
    manifest = plan.to_manifest()
    envelope = (
        f"{BESTPLAN_ENVELOPE_START}\n"
        + json.dumps({"version": 1, "manifest": manifest}, sort_keys=True)
        + f"\n{BESTPLAN_ENVELOPE_END}"
    )
    conn.execute(
        """INSERT INTO bestplan_plans (
            plan_id, version, created_at, session_id, profile, workspace,
            baseline_fingerprint, raw_plan_json, validated_manifest_json, state,
            approval_digest
        ) VALUES ('legacy', 1, 1, 's', 'coder', '/tmp/proof-work', 'base', ?, ?,
                  'completed_unverified', ?)""",
        (envelope, json.dumps(manifest, sort_keys=True), _manifest_digest(manifest)),
    )
    conn.commit()
    conn.close()

    store = _store(db_path)
    assert store.get_plan("legacy")["execution_protocol"] == 1
    event_columns = {
        row[1]
        for row in store._connection().execute("PRAGMA table_info(bestplan_proof_events)")
    }
    candidate_columns = {
        row[1]
        for row in store._connection().execute("PRAGMA table_info(bestplan_candidates)")
    }
    assert {
        "event_hash",
        "previous_hash",
        "operation_id",
        "payload_json",
        "payload_digest",
        "raw_output_sha256",
        "raw_output_kind",
        "raw_output_framed_sha256",
    } <= event_columns
    assert {
        "receipt_json",
        "receipt_digest",
        "attempt_id",
        "commit_oid",
        "base_oid",
        "approval_digest",
        "contract_digest",
        "source_snapshot_digest",
        "raw_output_kind",
        "raw_output_framed_sha256",
    } <= candidate_columns
    trigger_sql = " ".join(
        row[0]
        for row in store._connection().execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'bestplan_%'"
        )
        if row[0]
    ).casefold()
    assert "raise(abort" in trigger_sql
    assert "bestplan_" in trigger_sql
    assert "json_extract" not in trigger_sql
    with pytest.raises(sqlite3.IntegrityError, match="invalid bestplan proof event shape"):
        store._execute_write(
            lambda conn: conn.execute(
                "INSERT INTO bestplan_proof_events "
                "(plan_id, authority_epoch, event_seq) VALUES ('legacy', 'old', 1)"
            )
        )


def _insert_event_values(conn, proof, values):
    columns = tuple(proof._EVENT_COLUMNS)
    conn.execute(
        f"INSERT INTO bestplan_proof_events ({','.join(columns)}) "
        f"VALUES ({','.join('?' for _ in columns)})",
        tuple(values[name] for name in columns),
    )


def _replace_with_nullable_event_table(store, proof):
    def replace(conn):
        conn.execute("DROP TABLE bestplan_proof_events")
        definitions = ",".join(
            f"{name} {declaration}"
            for name, declaration in proof._EVENT_COLUMNS.items()
        )
        conn.execute(f"CREATE TABLE bestplan_proof_events ({definitions})")

    store._execute_write(replace)


def _advisory_event_values(proof, tmp_path, operation=991):
    source = _store(tmp_path / f"event-source-{operation}.db")
    _insert_v2_plan(source)
    receipt = proof.ProofLedger(source).append_advisory(
        plan_id="bp_proof",
        operation_id=_operation(operation),
        kind="runtime_advisory",
        raw_output={"status": "ok"},
        output_source="process",
    )
    values = receipt.to_dict()
    source.close()
    return values


def _rebind_event_payload(proof, values, payload):
    values["payload_json"] = proof._canonical_json(payload)
    values["payload_digest"] = proof._digest(
        values["payload_json"], proof.REDACTED_DIGEST_DOMAIN
    )
    fingerprint_values = dict(values)
    fingerprint_values.update(
        {"expected_epoch": None, "expected_seq": 0, "expected_hash": None}
    )
    values["operation_fingerprint"] = proof._operation_fingerprint(
        fingerprint_values
    )
    values["event_hash"] = proof._event_hash(values)
    return values


def _unsupported_stream_event(proof, tmp_path):
    values = _advisory_event_values(proof, tmp_path)
    values["stream"] = "bogus"
    fingerprint_values = dict(values)
    fingerprint_values.update(
        {"expected_epoch": None, "expected_seq": 0, "expected_hash": None}
    )
    values["operation_fingerprint"] = proof._operation_fingerprint(
        fingerprint_values
    )
    values["event_hash"] = proof._event_hash(values)
    return values


def test_event_shape_rejects_a_self_consistent_unsupported_stream(tmp_path):
    proof = _proof()
    db_path = tmp_path / "event-stream-fresh.db"
    store = _store(db_path)
    _insert_v2_plan(store)
    _replace_with_nullable_event_table(store, proof)
    store.close()
    store = _store(db_path)
    values = _unsupported_stream_event(proof, tmp_path)

    with pytest.raises(sqlite3.IntegrityError):
        store._execute_write(
            lambda conn: _insert_event_values(conn, proof, values)
        )


def test_verify_chain_rejects_an_inherited_unsupported_stream(tmp_path):
    proof = _proof()
    db_path = tmp_path / "event-stream-inherited.db"
    store = _store(db_path)
    _insert_v2_plan(store)
    _replace_with_nullable_event_table(store, proof)
    values = _unsupported_stream_event(proof, tmp_path)
    store._execute_write(
        lambda conn: _insert_event_values(conn, proof, values)
    )
    store.close()
    reopened = _store(db_path)

    with pytest.raises(proof.ProofValidationError):
        proof.ProofLedger(reopened).verify_chain("bp_proof")


@pytest.mark.parametrize("corruption", ("oversized", "extra_plaintext"))
def test_verify_chain_rejects_inherited_invalid_redacted_projection(
    tmp_path, corruption
):
    proof = _proof()
    db_path = tmp_path / f"event-payload-{corruption}.db"
    store = _store(db_path)
    _insert_v2_plan(store)
    _replace_with_nullable_event_table(store, proof)
    values = _advisory_event_values(proof, tmp_path, operation=992)
    payload = json.loads(values["payload_json"])
    secret = "inherited-raw-plaintext-sentinel"
    if corruption == "oversized":
        payload["summary"] = [0] * 20_000
    else:
        payload["raw_text"] = secret
    _rebind_event_payload(proof, values, payload)
    store._execute_write(
        lambda conn: _insert_event_values(conn, proof, values)
    )
    store.close()
    reopened = _store(db_path)

    with pytest.raises(proof.ProofValidationError):
        proof.ProofLedger(reopened).verify_chain("bp_proof")


def _replace_with_nullable_candidate_table(store, proof):
    def replace(conn):
        conn.execute("DROP TABLE bestplan_candidates")
        definitions = ",".join(
            f"{name} {declaration}"
            for name, declaration in proof._CANDIDATE_COLUMNS.items()
        )
        conn.execute(f"CREATE TABLE bestplan_candidates ({definitions})")

    store._execute_write(replace)


def _null_identity_candidate_values(proof, plan):
    redacted = proof.redact_output({"status": "frozen"}, source="candidate")
    body = {
        "schema": proof.CANDIDATE_SCHEMA,
        "version": 1,
        "plan_id": "bp_proof",
        "candidate_id": None,
        "slice_id": None,
        "attempt_id": None,
        "commit_oid": None,
        "tree_oid": None,
        "base_oid": plan["baseline_revision"],
        "approval_digest": plan["approval_digest"],
        "contract_digest": plan["promotion_contract_digest"],
        "source_snapshot_digest": plan["source_snapshot_digest"],
        "output": json.loads(redacted.canonical_json),
        "created_at_policy": "explicit",
        "created_at_ns": 1,
    }
    receipt_json = proof._canonical_json(body)
    return {
        "plan_id": "bp_proof",
        "candidate_id": None,
        "slice_id": None,
        "attempt_id": None,
        "commit_oid": None,
        "tree_oid": None,
        "base_oid": plan["baseline_revision"],
        "approval_digest": plan["approval_digest"],
        "contract_digest": plan["promotion_contract_digest"],
        "source_snapshot_digest": plan["source_snapshot_digest"],
        "receipt_json": receipt_json,
        "receipt_digest": proof._digest(
            receipt_json, proof.CANDIDATE_DIGEST_DOMAIN
        ),
        "raw_output_sha256": redacted.raw_sha256,
        "raw_output_kind": redacted.raw_kind,
        "raw_output_framed_sha256": redacted.raw_framed_sha256,
        "created_at_policy": "explicit",
        "created_at_ns": 1,
    }


def _insert_candidate_values(conn, proof, values):
    columns = tuple(proof._CANDIDATE_COLUMNS)
    conn.execute(
        f"INSERT INTO bestplan_candidates ({','.join(columns)}) "
        f"VALUES ({','.join('?' for _ in columns)})",
        tuple(values[name] for name in columns),
    )


def test_partial_candidate_shape_rejects_self_consistent_null_identity(tmp_path):
    proof = _proof()
    db_path = tmp_path / "candidate-null-fresh.db"
    store = _store(db_path)
    plan = _insert_v2_plan(store)
    _replace_with_nullable_candidate_table(store, proof)
    store.close()
    reopened = _store(db_path)
    values = _null_identity_candidate_values(proof, plan)

    with pytest.raises(sqlite3.IntegrityError):
        reopened._execute_write(
            lambda conn: _insert_candidate_values(conn, proof, values)
        )


@pytest.mark.parametrize("consumer", ("append", "verify"))
def test_inherited_null_candidate_identity_is_rejected(tmp_path, consumer):
    proof = _proof()
    db_path = tmp_path / "candidate-null-inherited.db"
    store = _store(db_path)
    plan = _insert_v2_plan(store)
    _replace_with_nullable_candidate_table(store, proof)
    values = _null_identity_candidate_values(proof, plan)
    store._execute_write(
        lambda conn: _insert_candidate_values(conn, proof, values)
    )
    store.close()
    reopened = _store(db_path)
    ledger = proof.ProofLedger(reopened)

    with pytest.raises(proof.ProofValidationError):
        if consumer == "append":
            _append(ledger, ensure_candidate=False)
        else:
            ledger.verify_chain("bp_proof")


@pytest.mark.parametrize("consumer", ("append", "verify"))
def test_inherited_candidate_receipt_rejects_extra_top_level_plaintext(
    tmp_path, consumer
):
    proof = _proof()
    db_path = tmp_path / f"candidate-extra-key-{consumer}.db"
    store = _store(db_path)
    _insert_v2_plan(store)
    candidate = _candidate(proof.ProofLedger(store))
    body = json.loads(candidate.receipt_json)
    sentinel = "inherited-raw-plaintext-sentinel"
    body["raw_text"] = sentinel
    receipt_json = proof._canonical_json(body)
    receipt_digest = proof._digest(
        receipt_json, proof.CANDIDATE_DIGEST_DOMAIN
    )

    def inherit_extra_key(conn):
        conn.execute("DROP TRIGGER bestplan_candidates_no_update_v1")
        conn.execute(
            "UPDATE bestplan_candidates SET receipt_json=?, receipt_digest=? "
            "WHERE plan_id=? AND candidate_id=?",
            (receipt_json, receipt_digest, candidate.plan_id, candidate.candidate_id),
        )

    store._execute_write(inherit_extra_key)
    store.close()
    reopened = _store(db_path)
    ledger = proof.ProofLedger(reopened)
    assert sentinel in reopened._connection().execute(
        "SELECT receipt_json FROM bestplan_candidates WHERE plan_id=?",
        (candidate.plan_id,),
    ).fetchone()[0]

    with pytest.raises(proof.ProofValidationError):
        if consumer == "append":
            _append(ledger, ensure_candidate=False)
        else:
            ledger.verify_chain(candidate.plan_id)


def test_migration_rejects_same_named_trigger_with_changed_quoted_literal(tmp_path):
    proof = _proof()
    db_path = tmp_path / "incompatible-trigger.db"
    store = _store(db_path)
    trigger_name = "bestplan_proof_authority_insert_guard_v1"
    expected_sql = proof._TRIGGER_SQL[trigger_name]
    incompatible_sql = expected_sql.replace("'authority'", "'AUTHORITY'", 1)
    assert incompatible_sql != expected_sql

    def replace_trigger(conn):
        conn.execute(f"DROP TRIGGER {trigger_name}")
        conn.execute(incompatible_sql)

    store._execute_write(replace_trigger)
    store.close()

    with pytest.raises(proof.ProofMigrationError, match="incompatible definition"):
        _store(db_path)


def test_chain_verification_detects_projection_or_event_corruption_after_trigger_removal(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "state.db")
    _insert_v2_plan(store)
    ledger = proof.ProofLedger(store)
    _append(ledger)
    store._execute_write(
        lambda conn: (
            conn.execute("DROP TRIGGER bestplan_proof_events_no_update_v1"),
            conn.execute(
                "UPDATE bestplan_proof_events SET integration_oid=? "
                "WHERE plan_id='bp_proof'",
                ("f" * 40,),
            ),
        )
    )

    with pytest.raises(proof.ProofValidationError, match="event hash"):
        ledger.verify_chain("bp_proof")


def test_chain_verification_rejects_noncanonical_payload_json(tmp_path):
    proof = _proof()
    store = _store(tmp_path / "state.db")
    _insert_v2_plan(store)
    ledger = proof.ProofLedger(store)
    event = _append(ledger)
    parsed = json.loads(event.payload_json)
    noncanonical = json.dumps(parsed, sort_keys=True, indent=2)
    store._execute_write(
        lambda conn: (
            conn.execute("DROP TRIGGER bestplan_proof_events_no_update_v1"),
            conn.execute(
                "UPDATE bestplan_proof_events SET payload_json=? "
                "WHERE plan_id='bp_proof'",
                (noncanonical,),
            ),
        )
    )

    with pytest.raises(proof.ProofValidationError, match="canonical"):
        ledger.verify_chain("bp_proof")
