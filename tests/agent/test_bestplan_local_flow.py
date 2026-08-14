from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def real_review_loop():
    """Leave the production review loop visible to focused loop tests."""


@pytest.fixture(autouse=True)
def _clean_review_pass(monkeypatch, request):
    from tools import delegate_tool

    if "real_review_loop" in request.fixturenames:
        return

    def pass_review(**kwargs):
        return SimpleNamespace(
            integration=kwargs["integration"],
            checks=kwargs["checks"],
            target=SimpleNamespace(target_digest="7" * 64, generation=0),
            receipt=SimpleNamespace(receipt_digest="8" * 64, passed=True),
            job_id="review-job-local",
            owner_id="review-worker",
            fencing_token=1,
        )

    monkeypatch.setattr(
        delegate_tool,
        "_run_local_bestplan_review_loop",
        pass_review,
        raising=False,
    )


def _completed(position: int):
    frozen = SimpleNamespace(candidate_id=f"candidate-{position}")
    spec = SimpleNamespace(candidate_id=f"candidate-{position}")
    return frozen, spec, f"slice-{position}"


def _runtime(tmp_path: Path):
    return SimpleNamespace(
        operation_timeout_seconds=30.0,
        integration_root=tmp_path / "integration",
        checks_root=tmp_path / "checks",
        check_runtime=object(),
        check_plan=SimpleNamespace(commands=("pytest-command",)),
    )


def _snapshot(tmp_path: Path):
    return SimpleNamespace(
        head_oid="0" * 40,
        repo=SimpleNamespace(workspace=str(tmp_path)),
    )


def _review_loop_inputs(tmp_path: Path):
    """Build strict-enough immutable inputs for the review-loop flow test."""

    import hashlib
    import json
    import subprocess

    from agent.bestplan_candidates import CandidateSpec, FrozenCandidate
    from agent.bestplan_contract import source_snapshot_digest
    from agent.bestplan_promotion import FrozenIntegration
    from agent.bestplan_source import (
        DEFAULT_SOURCE_OPERATION_SECONDS,
        capture_source_snapshot,
        resolve_repo_identity,
    )
    from agent.execution_plan import compile_execution_plan

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "review-loop@example.test")
    git("config", "user.name", "Review Loop Test")
    for name in ("slice-a", "slice-b"):
        directory = repo / name
        directory.mkdir()
        (directory / "result.txt").write_text("base\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "base")
    snapshot = capture_source_snapshot(
        resolve_repo_identity(str(repo)),
        time.monotonic() + DEFAULT_SOURCE_OPERATION_SECONDS,
    )
    plan = compile_execution_plan(
        {
            "version": 1,
            "mode": "delegate",
            "risk": "high",
            "slices": [
                {
                    "id": name,
                    "kind": "implement",
                    "goal": f"Implement {name}",
                    "depends_on": [],
                    "capability": "fast_fallback",
                    "workspace": str(repo),
                    "allowed_paths": [f"{name}/"],
                    "read_only": False,
                    "expected_artifacts": [f"{name}/result.txt"],
                    "acceptance": [f"verify {name}"],
                }
                for name in ("slice-a", "slice-b")
            ],
            "merge_policy": "apply independent candidates in manifest order",
            "stop_condition": "all acceptance conditions pass",
            "escalation_predicates": ["review_blocker"],
        }
    )
    controller = SimpleNamespace(
        controller_id="controller-v1",
        repository_id=snapshot.repo.repository_id,
        release_oid="a" * 40,
        artifact_sha256="b" * 64,
    )
    candidate_runtime = SimpleNamespace(
        controller=controller,
        controller_source=tmp_path / "controller",
        controller_python=tmp_path / "python",
        runtime_read_paths=(),
        attempts_root=tmp_path / "attempts",
        request_budget=8,
        token_budget=8192,
        max_iterations=8,
        max_output_tokens=1024,
        timeout_seconds=20.0,
        capability_ttl_seconds=60.0,
    )
    runtime = SimpleNamespace(
        candidate_runtime=candidate_runtime,
        operation_timeout_seconds=30.0,
        integration_root=tmp_path / "integration",
        checks_root=tmp_path / "checks",
        check_runtime=object(),
        check_plan=SimpleNamespace(commands=("pytest-command",)),
    )

    def frozen(spec: CandidateSpec, attempt_id: str, generation: int):
        receipt = {
            "candidate_id": spec.candidate_id,
            "generation": generation,
            "status": "completed",
        }
        encoded = json.dumps(
            receipt,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        return FrozenCandidate(
            candidate_id=spec.candidate_id,
            slice_id=spec.slice_id,
            attempt_id=attempt_id,
            commit_oid=hashlib.sha1(
                f"candidate-{spec.candidate_id}-{generation}".encode()
            ).hexdigest(),
            tree_oid=hashlib.sha1(
                f"tree-{spec.candidate_id}-{generation}".encode()
            ).hexdigest(),
            ref_name=(
                f"refs/hermes-bestplan/{spec.plan_id}/{spec.slice_id}/{attempt_id}"
            ),
            changed_paths=(spec.expected_artifacts[0].encode(),),
            raw_receipt=receipt,
            raw_receipt_sha256=hashlib.sha256(encoded).hexdigest(),
            policy_digest=hashlib.sha256(
                f"policy-{spec.candidate_id}-{generation}".encode()
            ).hexdigest(),
            controller_id=controller.controller_id,
            controller_repository_id=controller.repository_id,
            controller_release_oid=controller.release_oid,
            controller_artifact_sha256=controller.artifact_sha256,
            admitted_requests=1,
            admitted_input_tokens=32,
            admitted_output_tokens=16,
        )

    completed = []
    for index, name in enumerate(("slice-a", "slice-b")):
        spec = CandidateSpec(
            plan_id="bp-local",
            candidate_id=f"candidate-{index}",
            slice_id=name,
            goal=f"Implement {name}",
            allowed_paths=(f"{name}/",),
            read_only=False,
            expected_artifacts=(f"{name}/result.txt",),
            model=f"test/model-{index}",
            request_budget=8,
            token_budget=8192,
            expires_at=int(time.time()) + 600,
            max_iterations=8,
            max_output_tokens=1024,
            toolsets=("file",),
        )
        completed.append((frozen(spec, f"attempt-{index}", 0), spec, name))

    def integration(generation: int) -> FrozenIntegration:
        item = FrozenIntegration(
            plan_id="bp-local",
            approval_digest="5" * 64,
            contract_digest="6" * 64,
            source_snapshot_digest=source_snapshot_digest(snapshot),
            target_ref="refs/heads/main",
            target_oid=snapshot.head_oid,
            integration_oid=hashlib.sha1(
                f"integration-{generation}".encode()
            ).hexdigest(),
            tree_oid=hashlib.sha1(f"tree-{generation}".encode()).hexdigest(),
            ref_name=(
                f"refs/hermes-bestplan-integrations/bp-local/{generation}"
            ),
            candidates=(),
            receipt_digest=hashlib.sha256(
                f"integration-receipt-{generation}".encode()
            ).hexdigest(),
        )
        return item

    def checks(generation: int):
        return SimpleNamespace(
            integration_oid=integration(generation).integration_oid,
            contract_digest="6" * 64,
            receipt_digest=hashlib.sha256(
                f"check-receipt-{generation}".encode()
            ).hexdigest(),
        )

    return SimpleNamespace(
        snapshot=snapshot,
        plan=plan,
        runtime=runtime,
        completed=completed,
        integration=integration,
        checks=checks,
        frozen=frozen,
    )


def _review_generation_receipt(target, *, blocked: bool):
    """Create a strict receipt with two exact reviewer slots."""

    import hashlib

    from agent.review_engine import (
        FindingReproduction,
        ReviewFinding,
        ReviewGenerationReceipt,
        ReviewerReceipt,
        ReviewLocator,
        ReviewVerdict,
    )

    blocker = ReviewFinding(
        severity="high",
        locator=ReviewLocator(
            kind="changed_lines",
            path="slice-a/result.txt",
            start_line=1,
            end_line=1,
            quoted_evidence="broken\n",
            cited_bytes_sha256=hashlib.sha256(b"broken\n").hexdigest(),
        ),
        title="The exact generation still has a blocking defect",
        trigger="The changed path is used",
        observed_failure="The approved behavior fails",
        blast_radius="The implementation result",
        reproduction=FindingReproduction(
            kind="not_applicable",
            reason="The flow test supplies validated exact evidence",
        ),
        fingerprint=hashlib.sha256(
            f"blocker-{target.generation}".encode()
        ).hexdigest(),
    )
    receipts = []
    for slot, provider, model, family in (
        ("smart_reviewer", "anthropic", "claude-review", "claude"),
        ("code_worker", "openai", "gpt-review", "gpt"),
    ):
        findings = (blocker,) if blocked and slot == "smart_reviewer" else ()
        verdict = ReviewVerdict(
            target_digest=target.target_digest,
            integration_oid=target.integration_oid,
            findings=findings,
            blocking_findings=findings,
            passed=not findings,
        )
        receipts.append(
            ReviewerReceipt(
                slot=slot,
                provider=provider,
                model=model,
                model_family=family,
                output_digest=hashlib.sha256(
                    f"output-{slot}-{target.generation}".encode()
                ).hexdigest(),
                verdict=verdict,
            )
        )
    return ReviewGenerationReceipt(
        target_digest=target.target_digest,
        integration_oid=target.integration_oid,
        reviewer_receipts=tuple(receipts),
        blocking_findings=(blocker,) if blocked else (),
        passed=not blocked,
        receipt_digest=hashlib.sha256(
            f"review-receipt-{target.generation}".encode()
        ).hexdigest(),
    )


def test_review_loop_repairs_two_blocking_generations_then_persists_fresh_pass(
    tmp_path, monkeypatch, real_review_loop,
):
    import hashlib
    import inspect
    import json
    import sqlite3

    from agent import bestplan_candidates, bestplan_checks, bestplan_promotion
    from agent import bestplan_review, review_engine
    from agent.review_engine import (
        EvidenceContext,
        ReviewArtifact,
        ReviewStore,
        ReviewTarget,
        build_review_packet,
    )
    from tools import delegate_tool

    inputs = _review_loop_inputs(tmp_path)
    initial = inputs.integration(0)
    initial_checks = inputs.checks(0)
    events: list[tuple] = []
    disposition_history: list[tuple[dict[str, str], ...]] = []
    generation_by_oid = {
        inputs.integration(generation).integration_oid: generation
        for generation in range(3)
    }

    def build_bundle(**kwargs):
        integration = kwargs["integration"]
        checks = kwargs["checks"]
        generation = kwargs["generation"]
        disposition_history.append(tuple(kwargs["dispositions"]))
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
            task="Implement both approved slices",
            acceptance=("Both approved slices satisfy acceptance",),
            rules=("CRITICAL and HIGH findings block the generation",),
            issue_locator_catalog={},
            dispositions=(),
        )
        evidence = EvidenceContext(
            read_frozen_file=lambda _path: b"broken\n",
            diff_membership=lambda _path, _start, _end: True,
            approved_lease_paths=("slice-a/", "slice-b/"),
        )
        packet = build_review_packet(target, artifact=artifact)
        return bestplan_review.BestplanReviewBundle(
            target=target,
            artifact=artifact,
            evidence=evidence,
            packet=packet,
            diff_bytes=b"",
        )

    def run_review(target, runtimes, **kwargs):
        slots = tuple(
            item.slot if hasattr(item, "slot") else item["slot"]
            for item in runtimes
        )
        assert slots == ("smart_reviewer", "code_worker")
        generation = target.generation
        events.append((
            "review",
            generation,
            target.integration_oid,
            target.check_receipt_digest,
            target.target_digest,
        ))
        receipt = _review_generation_receipt(target, blocked=generation < 2)
        callback = kwargs.get("receipt_callback")
        if callback is not None:
            for reviewer_receipt in receipt.reviewer_receipts:
                callback(reviewer_receipt, "{}")
        return receipt

    def repair_candidate(**kwargs):
        prior_generation = generation_by_oid[
            kwargs["candidate_base"].integration_oid
        ]
        generation = prior_generation + 1
        spec = kwargs["spec"]
        lease = spec.allowed_paths[0]
        events.append((
            "repair",
            generation,
            lease,
            kwargs["candidate_base"].integration_oid,
            kwargs["authority_client"],
            spec.plan_id,
            spec.candidate_id,
            spec.slice_id,
            kwargs["attempt_id"],
        ))
        return inputs.frozen(spec, kwargs["attempt_id"], generation)

    def freeze_repair(**kwargs):
        generation = kwargs["generation"]
        prior = kwargs["prior"]
        events.append((
            "freeze",
            generation,
            prior.integration_oid,
            len(kwargs["candidates"]),
        ))
        return inputs.integration(generation)

    def run_checks(**kwargs):
        integration = kwargs["integration"]
        generation = generation_by_oid[integration.integration_oid]
        receipt = inputs.checks(generation)
        events.append((
            "checks",
            generation,
            integration.integration_oid,
            receipt.receipt_digest,
        ))
        return receipt

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

    candidate_authorities = ("candidate-authority-a", "candidate-authority-b")
    reviewer_authorities = (
        SimpleNamespace(
            slot="smart_reviewer",
            provider="anthropic",
            model="claude-review",
            model_family="claude",
            authority=object(),
        ),
        SimpleNamespace(
            slot="code_worker",
            provider="openai",
            model="gpt-review",
            model_family="gpt",
            authority=object(),
        ),
    )
    state_db_path = tmp_path / "state.db"

    signature = inspect.signature(delegate_tool._run_local_bestplan_review_loop)
    assert not {
        "max_rounds",
        "max_review_rounds",
        "max_generations",
        "review_round_limit",
    }.intersection(signature.parameters)

    result = delegate_tool._run_local_bestplan_review_loop(
        plan_id="bp-local",
        raw_request="Implement both approved slices",
        plan=inputs.plan,
        snapshot=inputs.snapshot,
        contract={"schema": "test.local-review", "commands": []},
        approval_digest="5" * 64,
        contract_digest="6" * 64,
        completed=inputs.completed,
        candidate_authorities=candidate_authorities,
        review_authority_bindings=reviewer_authorities,
        candidate_runtime_routes=(
            {
                "route": "code_worker",
                "provider": "provider-a",
                "model": "candidate-a",
                "runtime_fingerprint": "a" * 64,
                "toolsets": ["file"],
                "bestplan_toolsets": ["file"],
            },
            {
                "route": "deep_research",
                "provider": "provider-b",
                "model": "candidate-b",
                "runtime_fingerprint": "b" * 64,
                "toolsets": ["file"],
                "bestplan_toolsets": ["file"],
            },
        ),
        integration=initial,
        checks=initial_checks,
        runtime=inputs.runtime,
        state_db_path=state_db_path,
        session_id="session-local",
        profile="default",
        deadline=time.monotonic() + 30,
        cancel_event=None,
    )

    review_events = [item for item in events if item[0] == "review"]
    assert [item[1] for item in review_events] == [0, 1, 2]
    assert [item[2] for item in review_events] == [
        inputs.integration(index).integration_oid for index in range(3)
    ]
    assert [item[3] for item in review_events] == [
        inputs.checks(index).receipt_digest for index in range(3)
    ]
    assert len({item[4] for item in review_events}) == 3

    for generation in (1, 2):
        repairs = [
            item for item in events
            if item[0] == "repair" and item[1] == generation
        ]
        assert {item[2] for item in repairs} == {"slice-a"}
        assert {item[3] for item in repairs} == {
            inputs.integration(generation - 1).integration_oid
        }
        assert [item[4] for item in repairs] == [candidate_authorities[0]]
        assert len({item[5] for item in repairs}) == 1
        assert len({item[6] for item in repairs}) == 1
        assert len({item[7] for item in repairs}) == 1
        assert len({item[8] for item in repairs}) == 1
        freeze = next(
            item for item in events
            if item[0] == "freeze" and item[1] == generation
        )
        checks = next(
            item for item in events
            if item[0] == "checks" and item[1] == generation
        )
        review = next(
            item for item in events
            if item[0] == "review" and item[1] == generation
        )
        assert max(events.index(item) for item in repairs) < events.index(freeze)
        assert events.index(freeze) < events.index(checks) < events.index(review)
        assert freeze[2:] == (
            inputs.integration(generation - 1).integration_oid,
            1,
        )

    first_generation_repairs = {
        (item[5], item[6], item[7], item[8])
        for item in events
        if item[0] == "repair" and item[1] == 1
    }
    second_generation_repairs = {
        (item[5], item[6], item[7], item[8])
        for item in events
        if item[0] == "repair" and item[1] == 2
    }
    assert {item[0] for item in first_generation_repairs}.isdisjoint(
        {item[0] for item in second_generation_repairs}
    )
    assert {item[1] for item in first_generation_repairs}.isdisjoint(
        {item[1] for item in second_generation_repairs}
    )
    assert {item[2] for item in first_generation_repairs}.isdisjoint(
        {item[2] for item in second_generation_repairs}
    )
    assert {item[3] for item in first_generation_repairs}.isdisjoint(
        {item[3] for item in second_generation_repairs}
    )

    first_review = next(item for item in events if item[0] == "review")
    first_repair = next(item for item in events if item[0] == "repair")
    assert events.index(first_review) < events.index(first_repair)
    assert result.integration == inputs.integration(2)
    assert result.checks.receipt_digest == inputs.checks(2).receipt_digest
    assert result.target.generation == 2
    assert result.receipt.passed is True
    assert disposition_history[0] == ()
    for generation, dispositions in enumerate(disposition_history[1:], start=1):
        assert len(dispositions) == 1
        assert dispositions[0]["status"] == "fixed"
        assert "slice-a/result.txt" in dispositions[0]["evidence"]
        assert "slice-b/result.txt" not in dispositions[0]["evidence"]
        assert f"generation {generation}" in dispositions[0]["evidence"]

    durable = ReviewStore(state_db_path).latest_exact_pass(
        target=result.target,
        review_receipt_digest=result.receipt.receipt_digest,
    )
    assert durable is not None
    assert durable.generation == 2
    stored_events = ReviewStore(state_db_path).list_events(durable.job_id)
    assert [
        event.generation
        for event in stored_events
        if event.kind == "generation_started"
    ] == [0]
    assert [
        event.generation
        for event in stored_events
        if event.kind == "checks_passed"
    ] == [1, 2]
    job = ReviewStore(state_db_path).get_job(durable.job_id)
    stored_routes = json.loads(job.runtime_routes_json)
    assert [item["route"] for item in stored_routes] == [
        "candidate-0",
        "candidate-1",
        "smart_reviewer",
        "code_worker",
    ]
    assert [item["runtime_fingerprint"] for item in stored_routes[:2]] == [
        "a" * 64,
        "b" * 64,
    ]
    adapter_state = json.loads(job.adapter_state_json)
    assert adapter_state["initial_integration"]["integration_oid"] == (
        inputs.integration(0).integration_oid
    )
    assert adapter_state["initial_checks"]["receipt_digest"] == (
        inputs.checks(0).receipt_digest
    )
    with sqlite3.connect(state_db_path) as connection:
        repair_rows = connection.execute(
            "SELECT candidate_receipts_json FROM review_repair_checkpoints "
            "ORDER BY generation"
        ).fetchall()
        check_rows = connection.execute(
            "SELECT check_receipt_json FROM review_check_checkpoints "
            "ORDER BY generation"
        ).fetchall()
    assert [
        json.loads(row[0])[0]["integration"]["integration_oid"]
        for row in repair_rows
    ] == [inputs.integration(index).integration_oid for index in (1, 2)]
    assert [
        json.loads(row[0])["check_set"]["receipt_digest"]
        for row in check_rows
    ] == [inputs.checks(index).receipt_digest for index in (1, 2)]
    assert delegate_tool._bestplan_review_integration_from_payload(
        adapter_state["initial_integration"]
    ) == inputs.integration(0)
    restored_initial_checks = (
        delegate_tool._bestplan_review_check_set_from_payload(
            adapter_state["initial_checks"]
        )
    )
    assert restored_initial_checks.integration_oid == inputs.checks(0).integration_oid
    assert restored_initial_checks.receipt_digest == inputs.checks(0).receipt_digest
    assert delegate_tool._bestplan_review_integration_from_payload(
        json.loads(repair_rows[-1][0])[0]["integration"]
    ) == inputs.integration(2)
    restored_final_checks = delegate_tool._bestplan_review_check_set_from_payload(
        json.loads(check_rows[-1][0])["check_set"]
    )
    assert restored_final_checks.integration_oid == inputs.checks(2).integration_oid
    assert restored_final_checks.receipt_digest == inputs.checks(2).receipt_digest


def test_review_loop_refreshes_attempt_deadline_after_each_generation(
    tmp_path, monkeypatch, real_review_loop,
):
    from tools import delegate_tool

    inputs = _review_loop_inputs(tmp_path)
    monotonic_values = iter((10.0, 10.0, 11.0, 11.0, 12.0, 12.0, 13.0))
    monkeypatch.setattr(
        delegate_tool.time,
        "monotonic",
        lambda: next(monotonic_values, 13.0),
    )
    observed_deadlines = []

    def stop_after_deadline_capture(**kwargs):
        observed_deadlines.append(kwargs["deadline"])
        raise RuntimeError("deadline captured")

    monkeypatch.setattr(
        "agent.bestplan_review.build_bestplan_review_bundle",
        stop_after_deadline_capture,
    )
    reviewer_authorities = (
        SimpleNamespace(
            slot="smart_reviewer",
            provider="anthropic",
            model="claude-review",
            model_family="claude",
        ),
        SimpleNamespace(
            slot="code_worker",
            provider="openai",
            model="gpt-review",
            model_family="gpt",
        ),
    )

    with pytest.raises(RuntimeError, match="deadline captured"):
        delegate_tool._run_local_bestplan_review_loop(
            plan_id="bp-local",
            raw_request="Implement both approved slices",
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
            state_db_path=tmp_path / "state.db",
            session_id="session-local",
            profile="default",
            deadline=10.5,
            cancel_event=None,
        )

    # The original dispatch deadline can expire before a later review attempt.
    # The loop must derive a fresh bounded deadline from its runtime policy.
    assert observed_deadlines == [10.0 + inputs.runtime.operation_timeout_seconds]
    assert observed_deadlines[0] > 10.5


def test_local_batch_orders_proof_check_prepare_land_activate_and_prompt(
    tmp_path, monkeypatch,
):
    from agent import bestplan_checks, bestplan_local_git, bestplan_promotion
    from agent import bestplan_state
    from tools import delegate_tool

    events: list[object] = []
    completed = [_completed(0), _completed(1)]
    integration = SimpleNamespace(
        integration_oid="1" * 40,
        target_oid="0" * 40,
        tree_oid="2" * 40,
        receipt_digest="3" * 64,
    )
    checks = SimpleNamespace(receipt_digest="4" * 64)
    push_target = SimpleNamespace(
        display_url="example.invalid/repo",
        remote_ref="refs/heads/main",
    )
    landing = SimpleNamespace(new_oid=integration.integration_oid)

    def binding(**kwargs):
        events.append(("binding", kwargs["manifest_slice_id"]))
        return f"binding-{kwargs['manifest_slice_id']}"

    def freeze(**kwargs):
        events.append(("freeze", tuple(kwargs["candidates"])))
        return integration

    def run_checks(**kwargs):
        events.append(("checks", kwargs["integration"]))
        return checks

    def review(**kwargs):
        events.append(("review", kwargs["integration"].integration_oid))
        return SimpleNamespace(
            integration=kwargs["integration"],
            checks=kwargs["checks"],
            target=SimpleNamespace(target_digest="7" * 64, generation=0),
            receipt=SimpleNamespace(receipt_digest="8" * 64, passed=True),
            job_id="review-job-local",
            owner_id="review-worker",
            fencing_token=1,
        )

    def observe(**kwargs):
        events.append(("observe", kwargs["integration_oid"]))
        return push_target

    def land(**kwargs):
        events.append(("land", kwargs["checks"].receipt_digest))
        return landing

    class Store:
        def __init__(self, *, db_path):
            events.append(("store", Path(db_path)))

        def prepare_local_push(self, plan_id, **kwargs):
            events.append((
                "prepare",
                plan_id,
                kwargs["check_set_digest"],
                kwargs["review_target"].target_digest,
                kwargs["review_receipt_digest"],
                kwargs["expires_at"],
            ))
            return {"state": "prepared"}

        def claim_landing(self, plan_id, **kwargs):
            events.append(("claim", plan_id, kwargs["owner_id"]))
            return SimpleNamespace(authorization_digest="9" * 64)

        def activate_local_push(self, plan_id, *, landing_receipt):
            events.append(("activate", plan_id, landing_receipt.new_oid))
            return True

        def close(self):
            events.append("close")

    monkeypatch.setattr(delegate_tool, "_build_local_candidate_binding", binding)
    monkeypatch.setattr(bestplan_promotion, "freeze_integration", freeze)
    monkeypatch.setattr(bestplan_checks, "run_integration_checks", run_checks)
    monkeypatch.setattr(delegate_tool, "_run_local_bestplan_review_loop", review)
    monkeypatch.setattr(
        bestplan_local_git,
        "observe_prelanding_local_main_push_target",
        observe,
    )
    monkeypatch.setattr(bestplan_local_git, "land_checked_integration", land)
    monkeypatch.setattr(bestplan_state, "BestplanStore", Store)

    result = delegate_tool._finish_local_bestplan_batch(
        plan_id="bp-local",
        plan=object(),
        snapshot=_snapshot(tmp_path),
        contract={"commands": []},
        approval_digest="5" * 64,
        contract_digest="6" * 64,
        completed=completed,
        projected_results=[
            {"status": "frozen", "summary": "first"},
            {"status": "frozen", "summary": "second"},
        ],
        runtime=_runtime(tmp_path),
        state_db_path=tmp_path / "state.db",
        session_id="session-local",
        profile="default",
        cancel_event=None,
        now=1.0,
    )

    names = [item[0] if isinstance(item, tuple) else item for item in events]
    assert names == [
        "binding",
        "binding",
        "freeze",
        "checks",
        "review",
        "observe",
        "store",
        "prepare",
        "claim",
        "land",
        "activate",
        "close",
    ]
    assert events[0:2] == [("binding", "slice-0"), ("binding", "slice-1")]
    prepare = next(item for item in events if isinstance(item, tuple) and item[0] == "prepare")
    assert prepare[3:5] == ("7" * 64, "8" * 64)
    assert prepare[5] >= time.time() + 899
    assert "Reply `push` or `no`" in result["results"][-1]["summary"]
    assert "approved checks passed" in result["results"][-1]["summary"]
    assert "all required checks" not in result["results"][-1]["summary"]
    assert result["integration_oid"] == integration.integration_oid
    assert result["check_set_digest"] == checks.receipt_digest
    assert result["review_receipt_digest"] == "8" * 64


@pytest.mark.parametrize(
    "failure_message",
    (
        "review cancelled",
        "review deadline expired",
        "review provider unavailable",
        "review evidence invalid",
    ),
)
def test_local_batch_review_failure_has_no_push_or_main_effect(
    tmp_path, monkeypatch, failure_message,
):
    from agent import bestplan_checks, bestplan_local_git, bestplan_promotion
    from agent import bestplan_state
    from tools import delegate_tool

    monkeypatch.setattr(
        delegate_tool,
        "_build_local_candidate_binding",
        lambda **kwargs: kwargs["manifest_slice_id"],
    )
    integration = SimpleNamespace(
        integration_oid="1" * 40,
        target_oid="0" * 40,
        tree_oid="2" * 40,
        receipt_digest="3" * 64,
    )
    monkeypatch.setattr(
        bestplan_promotion, "freeze_integration", lambda **kwargs: integration,
    )
    monkeypatch.setattr(
        bestplan_checks,
        "run_integration_checks",
        lambda **kwargs: SimpleNamespace(receipt_digest="4" * 64),
    )
    monkeypatch.setattr(
        delegate_tool,
        "_run_local_bestplan_review_loop",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError(failure_message)),
    )
    monkeypatch.setattr(
        bestplan_local_git,
        "observe_prelanding_local_main_push_target",
        lambda **kwargs: pytest.fail("push target observed after blocked review"),
    )
    monkeypatch.setattr(
        bestplan_local_git,
        "land_checked_integration",
        lambda **kwargs: pytest.fail("local main changed after blocked review"),
    )

    class Store:
        def __init__(self, *, db_path):
            pytest.fail("push prepared after blocked review")

    monkeypatch.setattr(bestplan_state, "BestplanStore", Store)

    with pytest.raises(RuntimeError, match=failure_message):
        delegate_tool._finish_local_bestplan_batch(
            plan_id="bp-local",
            plan=object(),
            snapshot=_snapshot(tmp_path),
            contract={"commands": []},
            approval_digest="5" * 64,
            contract_digest="6" * 64,
            completed=[_completed(0)],
            projected_results=[{"status": "frozen", "summary": "first"}],
            runtime=_runtime(tmp_path),
            state_db_path=tmp_path / "state.db",
            session_id="session-local",
            profile="default",
            cancel_event=None,
            now=time.time(),
        )


def test_local_batch_check_failure_has_no_push_or_main_effect(tmp_path, monkeypatch):
    from agent import bestplan_checks, bestplan_local_git, bestplan_promotion
    from tools import delegate_tool

    monkeypatch.setattr(
        delegate_tool,
        "_build_local_candidate_binding",
        lambda **kwargs: kwargs["manifest_slice_id"],
    )
    monkeypatch.setattr(
        bestplan_promotion,
        "freeze_integration",
        lambda **kwargs: SimpleNamespace(integration_oid="1" * 40),
    )
    monkeypatch.setattr(
        bestplan_checks,
        "run_integration_checks",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("checks failed")),
    )
    monkeypatch.setattr(
        bestplan_local_git,
        "observe_prelanding_local_main_push_target",
        lambda **kwargs: pytest.fail("push target observed after failed checks"),
    )
    monkeypatch.setattr(
        bestplan_local_git,
        "land_checked_integration",
        lambda **kwargs: pytest.fail("local main changed after failed checks"),
    )

    with pytest.raises(RuntimeError, match="checks failed"):
        delegate_tool._finish_local_bestplan_batch(
            plan_id="bp-local",
            plan=object(),
            snapshot=_snapshot(tmp_path),
            contract={"commands": []},
            approval_digest="5" * 64,
            contract_digest="6" * 64,
            completed=[_completed(0)],
            projected_results=[{"status": "frozen", "summary": "first"}],
            runtime=_runtime(tmp_path),
            state_db_path=tmp_path / "state.db",
            session_id="session-local",
            profile="default",
            cancel_event=None,
            now=time.time(),
        )


def test_initial_deterministic_check_failure_repairs_rechecks_reviews_then_lands(
    tmp_path, monkeypatch, real_review_loop,
):
    """A failed first check is a repair input, not a terminal batch result."""

    import hashlib
    import json

    from agent import (
        bestplan_candidates,
        bestplan_checks,
        bestplan_local,
        bestplan_promotion,
        bestplan_review,
    )
    from agent.review_engine import (
        EvidenceContext,
        ReviewArtifact,
        ReviewStore,
        ReviewTarget,
        build_review_packet,
    )
    from tools import delegate_tool

    inputs = _review_loop_inputs(tmp_path)
    initial = inputs.integration(0)
    repaired = inputs.integration(1)
    repaired_checks = inputs.checks(1)
    events: list[tuple[object, ...]] = []

    monkeypatch.setattr(
        bestplan_promotion,
        "freeze_integration",
        lambda **_kwargs: initial,
    )

    def run_checks(**kwargs):
        integration = kwargs["integration"]
        if integration.integration_oid == initial.integration_oid:
            events.append(("initial_checks_failed", integration.integration_oid))
            raise bestplan_checks.CheckExecutionError(
                "enrollment-bound check returned nonzero"
            )
        assert integration.integration_oid == repaired.integration_oid
        events.append(("fresh_checks_passed", integration.integration_oid))
        return repaired_checks

    def repair_candidate(**kwargs):
        spec = kwargs["spec"]
        events.append(("repair", spec.allowed_paths[0]))
        return inputs.frozen(spec, kwargs["attempt_id"], 1)

    def freeze_repair(**kwargs):
        events.append(("repair_frozen", kwargs["generation"]))
        assert kwargs["prior"].integration_oid == initial.integration_oid
        assert kwargs["generation"] == 1
        return repaired

    def build_bundle(**kwargs):
        integration = kwargs["integration"]
        checks = kwargs["checks"]
        generation = kwargs["generation"]
        assert generation == 1
        assert integration.integration_oid == repaired.integration_oid
        assert checks.receipt_digest == repaired_checks.receipt_digest
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
            task="Repair the deterministic check failure",
            acceptance=("The approved checks pass",),
            rules=("Only a fresh two-review pass can land",),
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

    def reviewer_call(_binding, request):
        packet = json.loads(request["messages"][1]["content"])
        events.append(("review", packet["target"]["generation"]))
        return json.dumps(
            {
                "schema": "hermes.bestplan.review-verdict.v1",
                "target_digest": packet["target_digest"],
                "integration_oid": packet["target"]["integration_oid"],
                "findings": [],
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def land(**kwargs):
        reviewed = kwargs["reviewed"]
        events.append(("land", reviewed.target.generation))
        assert reviewed.target.generation == 1
        assert reviewed.checks.receipt_digest == repaired_checks.receipt_digest
        assert reviewed.receipt.passed is True
        assert events.index(("fresh_checks_passed", repaired.integration_oid)) < (
            len(events) - 1
        )
        assert [item for item in events if item[0] == "review"] == [
            ("review", 1),
            ("review", 1),
        ]
        return {
            "results": list(kwargs["projected_results"]),
            "integration_oid": repaired.integration_oid,
            "local_main_oid": repaired.integration_oid,
        }

    monkeypatch.setattr(
        bestplan_checks, "run_integration_checks", run_checks,
    )
    monkeypatch.setattr(
        bestplan_candidates,
        "run_and_freeze_repair_candidate",
        repair_candidate,
    )
    monkeypatch.setattr(
        bestplan_promotion,
        "freeze_repair_integration",
        freeze_repair,
    )
    monkeypatch.setattr(
        bestplan_review, "build_bestplan_review_bundle", build_bundle,
    )
    monkeypatch.setattr(
        bestplan_local, "call_local_review_authority", reviewer_call,
    )
    monkeypatch.setattr(
        delegate_tool, "_land_reviewed_local_bestplan", land,
    )

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
    result = delegate_tool._finish_local_bestplan_batch(
        plan_id="bp-local",
        raw_request="Repair the deterministic check failure",
        plan=inputs.plan,
        snapshot=inputs.snapshot,
        contract={"schema": "test.local-review", "commands": []},
        approval_digest="5" * 64,
        contract_digest="6" * 64,
        completed=inputs.completed,
        projected_results=[{"status": "frozen", "summary": "initial"}],
        runtime=inputs.runtime,
        state_db_path=tmp_path / "state.db",
        session_id="session-local",
        profile="default",
        cancel_event=None,
        candidate_authorities=(object(), object()),
        review_authority_bindings=reviewer_authorities,
        candidate_runtime_routes=(
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
        ),
        now=time.time(),
    )

    assert result["integration_oid"] == repaired.integration_oid
    assert events[0] == ("initial_checks_failed", initial.integration_oid)
    assert [item[0] for item in events].count("land") == 1
    review_store = ReviewStore(tmp_path / "state.db")
    job = review_store.get_job(
        delegate_tool._bestplan_safe_identifier("review-job", "bp-local")
    )
    assert job.state == "passed"
    durable_events = review_store.list_events(job.job_id)
    assert any(item.kind == "initial_checks_failed" for item in durable_events)
    assert any(item.kind == "checks_passed" for item in durable_events)
    assert any(item.kind == "review_pass" for item in durable_events)


def test_local_batch_requires_durable_prepare_before_local_main(tmp_path, monkeypatch):
    from agent import bestplan_checks, bestplan_local_git, bestplan_promotion
    from agent import bestplan_state
    from tools import delegate_tool

    monkeypatch.setattr(
        delegate_tool,
        "_build_local_candidate_binding",
        lambda **kwargs: kwargs["manifest_slice_id"],
    )
    integration = SimpleNamespace(
        integration_oid="1" * 40,
        target_oid="0" * 40,
    )
    monkeypatch.setattr(
        bestplan_promotion, "freeze_integration", lambda **kwargs: integration,
    )
    monkeypatch.setattr(
        bestplan_checks,
        "run_integration_checks",
        lambda **kwargs: SimpleNamespace(receipt_digest="4" * 64),
    )
    monkeypatch.setattr(
        bestplan_local_git,
        "observe_prelanding_local_main_push_target",
        lambda **kwargs: SimpleNamespace(display_url="example.invalid/repo"),
    )
    monkeypatch.setattr(
        bestplan_local_git,
        "land_checked_integration",
        lambda **kwargs: pytest.fail("local main changed without durable prepare"),
    )

    class Store:
        def __init__(self, *, db_path):
            pass

        def prepare_local_push(self, plan_id, **kwargs):
            return None

        def close(self):
            pass

    monkeypatch.setattr(bestplan_state, "BestplanStore", Store)

    with pytest.raises(delegate_tool.BestplanCandidateBatchError):
        delegate_tool._finish_local_bestplan_batch(
            plan_id="bp-local",
            plan=object(),
            snapshot=_snapshot(tmp_path),
            contract={"commands": []},
            approval_digest="5" * 64,
            contract_digest="6" * 64,
            completed=[_completed(0)],
            projected_results=[{"status": "frozen", "summary": "first"}],
            runtime=_runtime(tmp_path),
            state_db_path=tmp_path / "state.db",
            session_id="session-local",
            profile="default",
            cancel_event=None,
            now=time.time(),
        )


@pytest.mark.parametrize(
    ("classification", "expected_error"),
    (
        ("expected", "landing"),
        ("integration", None),
        ("other", "stale"),
        ("unavailable", "unknown"),
    ),
)
def test_local_batch_classifies_a_known_landing_failure_after_git_returns(
    tmp_path, monkeypatch, classification, expected_error,
):
    from agent import bestplan_checks, bestplan_local_git, bestplan_promotion
    from agent import bestplan_state
    from tools import delegate_tool

    events: list[object] = []
    integration = SimpleNamespace(
        integration_oid="1" * 40,
        target_oid="0" * 40,
    )
    checks = SimpleNamespace(receipt_digest="4" * 64)

    monkeypatch.setattr(
        delegate_tool,
        "_build_local_candidate_binding",
        lambda **kwargs: kwargs["manifest_slice_id"],
    )
    monkeypatch.setattr(
        bestplan_promotion, "freeze_integration", lambda **kwargs: integration,
    )
    monkeypatch.setattr(
        bestplan_checks, "run_integration_checks", lambda **kwargs: checks,
    )
    monkeypatch.setattr(
        bestplan_local_git,
        "observe_prelanding_local_main_push_target",
        lambda **kwargs: SimpleNamespace(
            display_url="example.invalid/repo",
            remote_ref="refs/heads/main",
        ),
    )

    def land(**_kwargs):
        events.append("land_returned")
        raise bestplan_local_git.LocalMainConflict("landing failed")

    def classify(**_kwargs):
        assert events[-1] == "land_returned"
        events.append(("classified", classification))
        return classification

    monkeypatch.setattr(bestplan_local_git, "land_checked_integration", land)
    monkeypatch.setattr(
        bestplan_local_git, "classify_local_main_for_push", classify,
    )

    class Store:
        def __init__(self, *, db_path):
            events.append(("store", Path(db_path)))

        def prepare_local_push(self, plan_id, **_kwargs):
            events.append(("prepare", plan_id))
            return {"state": "prepared"}

        def claim_landing(self, plan_id, **_kwargs):
            events.append(("claim", plan_id))
            return SimpleNamespace(
                authorization_digest="9" * 64,
                release_effect_lock=lambda: None,
            )

        def mark_landing_observation_pending(self, plan_id, **_kwargs):
            events.append(("observation-pending", plan_id))
            return True

        def recover_landing_claim(self, plan_id, **_kwargs):
            events.append(("recover-pre-effect", plan_id))
            return SimpleNamespace(status="retry_pre_effect")

        def activate_local_push(self, plan_id, **_kwargs):
            events.append(("activate", plan_id))
            return True

        def close(self):
            events.append("close")

    monkeypatch.setattr(bestplan_state, "BestplanStore", Store)

    def run_batch():
        return delegate_tool._finish_local_bestplan_batch(
            plan_id="bp-local",
            plan=object(),
            snapshot=_snapshot(tmp_path),
            contract={"commands": []},
            approval_digest="5" * 64,
            contract_digest="6" * 64,
            completed=[_completed(0)],
            projected_results=[{"status": "frozen", "summary": "first"}],
            runtime=_runtime(tmp_path),
            state_db_path=tmp_path / "state.db",
            session_id="session-local",
            profile="default",
            cancel_event=None,
            now=time.time(),
        )

    if expected_error == "landing":
        with pytest.raises(
            bestplan_local_git.LocalMainConflict, match="landing failed",
        ):
            run_batch()
        assert events[-5:] == [
            "land_returned",
            ("classified", classification),
            ("observation-pending", "bp-local"),
            ("recover-pre-effect", "bp-local"),
            "close",
        ]
    elif expected_error == "stale":
        with pytest.raises(bestplan_local_git.LocalPushStale):
            run_batch()
        assert events[-4:] == [
            "land_returned",
            ("classified", classification),
            ("observation-pending", "bp-local"),
            "close",
        ]
    elif expected_error == "unknown":
        with pytest.raises(bestplan_local_git.LocalMainEffectUnknown):
            run_batch()
        assert events[-4:] == [
            "land_returned",
            ("classified", classification),
            ("observation-pending", "bp-local"),
            "close",
        ]
    else:
        result = run_batch()
        assert result["local_main_oid"] == integration.integration_oid
        assert events[-4:] == [
            "land_returned",
            ("classified", classification),
            ("activate", "bp-local"),
            "close",
        ]


def test_local_batch_leaves_an_unknown_landing_effect_prepared(
    tmp_path, monkeypatch,
):
    from agent import bestplan_checks, bestplan_local_git, bestplan_promotion
    from agent import bestplan_state
    from tools import delegate_tool

    integration = SimpleNamespace(
        integration_oid="1" * 40,
        target_oid="0" * 40,
    )
    monkeypatch.setattr(
        delegate_tool,
        "_build_local_candidate_binding",
        lambda **kwargs: kwargs["manifest_slice_id"],
    )
    monkeypatch.setattr(
        bestplan_promotion, "freeze_integration", lambda **kwargs: integration,
    )
    monkeypatch.setattr(
        bestplan_checks,
        "run_integration_checks",
        lambda **kwargs: SimpleNamespace(receipt_digest="4" * 64),
    )
    monkeypatch.setattr(
        bestplan_local_git,
        "observe_prelanding_local_main_push_target",
        lambda **kwargs: SimpleNamespace(
            display_url="example.invalid/repo",
            remote_ref="refs/heads/main",
        ),
    )
    monkeypatch.setattr(
        bestplan_local_git,
        "land_checked_integration",
        lambda **kwargs: (_ for _ in ()).throw(
            bestplan_local_git.LocalMainEffectUnknown("landing outcome unknown")
        ),
    )
    monkeypatch.setattr(
        bestplan_local_git,
        "classify_local_main_for_push",
        lambda **kwargs: pytest.fail("an unknown effect must remain prepared"),
    )

    class Store:
        def __init__(self, *, db_path):
            pass

        def prepare_local_push(self, plan_id, **_kwargs):
            return {"state": "prepared"}

        def claim_landing(self, _plan_id, **_kwargs):
            return SimpleNamespace(
                authorization_digest="9" * 64,
                release_effect_lock=lambda: None,
            )

        def _set_local_push_state(self, *_args, **_kwargs):
            raise AssertionError("an unknown effect must remain prepared")

        def close(self):
            pass

    monkeypatch.setattr(bestplan_state, "BestplanStore", Store)

    with pytest.raises(
        bestplan_local_git.LocalMainEffectUnknown,
        match="landing outcome unknown",
    ):
        delegate_tool._finish_local_bestplan_batch(
            plan_id="bp-local",
            plan=object(),
            snapshot=_snapshot(tmp_path),
            contract={"commands": []},
            approval_digest="5" * 64,
            contract_digest="6" * 64,
            completed=[_completed(0)],
            projected_results=[{"status": "frozen", "summary": "first"}],
            runtime=_runtime(tmp_path),
            state_db_path=tmp_path / "state.db",
            session_id="session-local",
            profile="default",
            cancel_event=None,
            now=time.time(),
        )


def test_local_batch_cancellation_after_prepare_closes_the_known_empty_effect(
    tmp_path, monkeypatch,
):
    from agent import bestplan_checks, bestplan_local_git, bestplan_promotion
    from agent import bestplan_state
    from tools import delegate_tool

    cancel_event = threading.Event()
    transitions: list[tuple[str, str]] = []
    integration = SimpleNamespace(
        integration_oid="1" * 40,
        target_oid="0" * 40,
    )
    monkeypatch.setattr(
        delegate_tool,
        "_build_local_candidate_binding",
        lambda **kwargs: kwargs["manifest_slice_id"],
    )
    monkeypatch.setattr(
        bestplan_promotion, "freeze_integration", lambda **kwargs: integration,
    )
    monkeypatch.setattr(
        bestplan_checks,
        "run_integration_checks",
        lambda **kwargs: SimpleNamespace(receipt_digest="4" * 64),
    )
    monkeypatch.setattr(
        bestplan_local_git,
        "observe_prelanding_local_main_push_target",
        lambda **kwargs: SimpleNamespace(
            display_url="example.invalid/repo",
            remote_ref="refs/heads/main",
        ),
    )
    monkeypatch.setattr(
        bestplan_local_git,
        "land_checked_integration",
        lambda **kwargs: pytest.fail("cancelled work must not launch Git"),
    )

    class Store:
        def __init__(self, *, db_path):
            pass

        def prepare_local_push(self, plan_id, **_kwargs):
            cancel_event.set()
            return {"state": "prepared"}

        def _set_local_push_state(self, _plan_id, **kwargs):
            transitions.append((kwargs["expected_state"], kwargs["new_state"]))
            return True

        def close(self):
            pass

    monkeypatch.setattr(bestplan_state, "BestplanStore", Store)

    with pytest.raises(delegate_tool.BestplanCandidateBatchError):
        delegate_tool._finish_local_bestplan_batch(
            plan_id="bp-local",
            plan=object(),
            snapshot=_snapshot(tmp_path),
            contract={"commands": []},
            approval_digest="5" * 64,
            contract_digest="6" * 64,
            completed=[_completed(0)],
            projected_results=[{"status": "frozen", "summary": "first"}],
            runtime=_runtime(tmp_path),
            state_db_path=tmp_path / "state.db",
            session_id="session-local",
            profile="default",
            cancel_event=cancel_event,
            now=time.time(),
        )

    assert transitions == [("prepared", "not_landed")]
