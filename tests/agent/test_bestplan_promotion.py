from __future__ import annotations

import base64
import copy
import hashlib
import inspect
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest


def test_completed_git_process_with_extinct_group_skips_slow_tree_observer(
    tmp_path, monkeypatch,
):
    promotion = _promotion()

    class _CompletedGit:
        pid = 424242
        stdin = None
        stdout = io.BytesIO(b"ok\n")
        stderr = io.BytesIO(b"")
        returncode = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(promotion.subprocess, "Popen", lambda *args, **kwargs: _CompletedGit())

    def extinct_group(_pgid, _sig):
        raise ProcessLookupError

    monkeypatch.setattr(promotion.os, "killpg", extinct_group)
    monkeypatch.setattr(
        promotion,
        "terminate_process_group",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("extinct Git group entered the slow process-tree observer")
        ),
    )
    repo = type(
        "Repo",
        (),
        {"git_dir": str(tmp_path), "worktree": str(tmp_path)},
    )()

    result = promotion._run_git(
        repo,
        "version",
        deadline=time.monotonic() + 5,
    )

    assert result.returncode == 0
    assert result.stdout == b"ok\n"


def _git(
    repo: Path,
    *args: str,
    input_text: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> str:
    environment = os.environ.copy()
    environment.update(extra_env or {})
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        input=input_text,
        env=environment,
    ).stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "promotion@example.test")
    _git(path, "config", "user.name", "Promotion Test")
    files = {
        "config/gate.txt": "gate-v1\n",
        "one/base.txt": "one-base\n",
        "two/base.txt": "two-base\n",
        "shared.txt": "shared-base\n",
    }
    for logical_path, content in files.items():
        destination = path / logical_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "base")
    return path


def _snapshot(repo: Path):
    from agent import bestplan_source as source

    identity = source.resolve_repo_identity(str(repo))
    return source.capture_source_snapshot(
        identity,
        time.monotonic() + source.DEFAULT_SOURCE_OPERATION_SECONDS,
    )


def _slice(
    slice_id: str,
    *,
    allowed_paths: tuple[str, ...] | None = None,
    expected_artifacts: tuple[str, ...] | None = None,
    depends_on: tuple[str, ...] = (),
) -> dict[str, object]:
    allowed = allowed_paths or (f"{slice_id}/",)
    artifacts = expected_artifacts or (f"{slice_id}/result.txt",)
    return {
        "id": slice_id,
        "kind": "implement",
        "goal": f"Implement {slice_id}",
        "depends_on": list(depends_on),
        "capability": "fast_fallback",
        "workspace": "",
        "allowed_paths": list(allowed),
        "read_only": False,
        "expected_artifacts": list(artifacts),
        "acceptance": [f"{slice_id} artifact is exact"],
    }


def _plan(repo: Path, *slices: dict[str, object]):
    from agent.execution_plan import compile_execution_plan

    manifest = {
        "version": 1,
        "mode": "delegate",
        "risk": "high",
        "slices": list(slices or (_slice("one"),)),
        "merge_policy": "apply independent candidates in manifest order",
        "stop_condition": "all acceptance conditions pass",
        "escalation_predicates": ["integration_conflict"],
    }
    for item in manifest["slices"]:
        item["workspace"] = str(repo)
    return compile_execution_plan(manifest)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _task5_safe_identifier(prefix: str, *values: object) -> str:
    payload = json.dumps(
        [str(value) for value in values],
        ensure_ascii=True,
        sort_keys=False,
        separators=(",", ":"),
    ).encode("ascii")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:24]}"


def _task5_candidate_plan_id(plan_id: str) -> str:
    return (
        plan_id
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", plan_id)
        else _task5_safe_identifier("plan", plan_id)
    )


def _bound_command(repo: Path, identifier: str):
    from agent.bestplan_contract import BoundCommand, PinnedInput

    executable = Path(sys.executable).resolve()
    return BoundCommand(
        identifier=identifier,
        executable=str(executable),
        executable_sha256=_sha256(executable),
        argv=("-c", "raise SystemExit(0)"),
        logical_cwd="integration",
        env=(("PYTHONHASHSEED", "0"),),
        inputs=(PinnedInput("config/gate.txt", _sha256(repo / "config/gate.txt")),),
        cache=(),
        timeout_seconds=30,
        network_allowlist=(),
    )


def _contract(snapshot, plan, repo: Path) -> dict[str, object]:
    from agent.bestplan_contract import (
        BlockingReview,
        ControllerIdentity,
        EnrolledRepository,
        Enrollment,
        LiveTarget,
        Publication,
        RollbackTarget,
        build_execution_contract,
    )

    check = _bound_command(repo, "focused-tests")
    review = _bound_command(repo, "review")
    activation = _bound_command(repo, "activation")
    health = _bound_command(repo, "health")
    canary = _bound_command(repo, "canary")
    rollback_command = _bound_command(repo, "rollback")
    repository_id = snapshot.repo.repository_id
    enrollment = Enrollment(
        reference="test-enrollment",
        enrollment_id="enrollment-1",
        revision=1,
        epoch="epoch-1",
        repository=EnrolledRepository.from_repo_identity(snapshot.repo),
        source_policy="head_only",
        capture_budget_seconds=30,
        local_ref="refs/heads/main",
        publication=Publication(
            repository_id=repository_id,
            remote_name="origin",
            push_url=str((repo.parent / "remote.git").resolve()),
            remote_ref="refs/heads/main",
            observed_oid=snapshot.head_oid,
        ),
        commands=(check,),
        review=BlockingReview(
            lane="smart_reviewer",
            command=review,
            blocking_severities=("critical", "high"),
        ),
        live_targets=(
            LiveTarget(
                repository_id=repository_id,
                adapter="test-adapter",
                target_id="test-target",
                service="test-service",
                activation=activation,
                health=health,
                canary=canary,
                rollback=RollbackTarget(
                    repository_id=repository_id,
                    selector=str((repo.parent / "release-selector").resolve()),
                    service="test-service",
                    command=rollback_command,
                ),
            ),
        ),
        controller=ControllerIdentity(
            repository_id=repository_id,
            controller_id="controller-n-1",
            release_oid=snapshot.head_oid,
            artifact_sha256="c" * 64,
        ),
        promotion_mode="auto_live",
    )
    return build_execution_contract(plan, snapshot, enrollment)


def _freeze_candidate(
    tmp_path: Path,
    snapshot,
    *,
    plan_id: str,
    slice_id: str,
    candidate_id: str | None = None,
    changes: dict[str, str] | None = None,
    allowed_paths: tuple[str, ...] | None = None,
    expected_artifacts: tuple[str, ...] | None = None,
    manifest_index: int = 0,
):
    from agent import bestplan_candidates as candidates

    manifest_slice_id = slice_id
    safe_slice_id = _task5_safe_identifier(
        "slice", plan_id, manifest_index, manifest_slice_id,
    )
    exact_candidate_id = _task5_safe_identifier(
        "candidate", plan_id, manifest_index, manifest_slice_id,
    )
    candidate_id = candidate_id or exact_candidate_id
    attempt_id = _task5_safe_identifier(
        "attempt", plan_id, manifest_index, manifest_slice_id,
    )
    changes = changes or {f"{slice_id}/result.txt": f"{slice_id}-result\n"}
    allowed_paths = allowed_paths or (f"{slice_id}/",)
    expected_artifacts = expected_artifacts or (f"{slice_id}/result.txt",)
    spec = candidates.CandidateSpec(
        plan_id=_task5_candidate_plan_id(plan_id),
        candidate_id=candidate_id,
        slice_id=safe_slice_id,
        goal=f"Create {manifest_slice_id} artifact",
        allowed_paths=allowed_paths,
        read_only=False,
        expected_artifacts=expected_artifacts,
        model="test/model",
        request_budget=2,
        token_budget=2048,
        expires_at=int(time.time()) + 600,
        max_iterations=5,
        max_output_tokens=512,
        toolsets=("file",),
    )
    attempt = candidates.create_candidate_attempt(
        snapshot,
        plan_id=spec.plan_id,
        slice_id=spec.slice_id,
        attempts_root=tmp_path / "attempts",
        attempt_id=attempt_id,
    )
    for logical_path, content in changes.items():
        destination = attempt.source_dir / logical_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    sealed = candidates.seal_candidate_attempt(attempt)
    return candidates._freeze_sealed_candidate_for_test(
        snapshot,
        sealed,
        spec,
        raw_receipt={
            "candidate_id": candidate_id,
            "manifest_slice_id": manifest_slice_id,
            "status": "completed",
        },
    )


def _promotion():
    from agent import bestplan_promotion as promotion

    return promotion


def _task3_candidate_receipt_digest(candidate) -> str:
    return hashlib.sha256(
        b"hermes.bestplan.task3-candidate-receipt.test.v1\0"
        + candidate.raw_receipt_sha256.encode("ascii")
    ).hexdigest()


def _task6_contract_digest(contract) -> str:
    from agent.bestplan_contract import contract_digest
    from agent.bestplan_local import (
        LOCAL_GO_CONTRACT_SCHEMA,
        local_go_contract_digest,
    )

    if contract.get("schema") == LOCAL_GO_CONTRACT_SCHEMA:
        return local_go_contract_digest(contract)
    return contract_digest(contract)


def _local_manifest_digest(plan) -> str:
    from agent.bestplan_contract import canonical_json

    return hashlib.sha256(
        canonical_json(plan.to_manifest()).encode("utf-8")
    ).hexdigest()


def _raw_local_approval_digest(plan, contract) -> str:
    from agent.bestplan_contract import canonical_json
    from agent.bestplan_local import local_go_contract_json

    manifest_json = canonical_json(plan.to_manifest()).encode("utf-8")
    return hashlib.sha256(
        b"hermes.bestplan.local-go-approval.v1\0"
        + manifest_json
        + b"\0"
        + local_go_contract_json(contract).encode("utf-8")
    ).hexdigest()


def _local_approval_digest(plan, contract) -> str:
    from agent.bestplan_contract import canonical_json

    manifest_json = canonical_json(plan.to_manifest()).encode("utf-8")
    assert contract["manifest_digest"] == hashlib.sha256(manifest_json).hexdigest()
    return _raw_local_approval_digest(plan, contract)


def _binding(
    promotion,
    snapshot,
    contract,
    approved: str,
    candidate,
    *,
    candidate_receipt_digest: str | None = None,
):
    from agent.bestplan_contract import source_snapshot_digest

    controller = contract["controller"]
    values = {
        "manifest_slice_id": candidate.raw_receipt["manifest_slice_id"],
        "candidate_id": candidate.candidate_id,
        "slice_id": candidate.slice_id,
        "attempt_id": candidate.attempt_id,
        "ref_name": candidate.ref_name,
        "commit_oid": candidate.commit_oid,
        "tree_oid": candidate.tree_oid,
        "changed_paths": candidate.changed_paths,
        "base_oid": snapshot.head_oid,
        "approval_digest": approved,
        "contract_digest": _task6_contract_digest(contract),
        "source_snapshot_digest": source_snapshot_digest(snapshot),
        "policy_digest": "b" * 64,
        "controller_id": controller["controller_id"],
        "controller_repository_id": controller["repository_id"],
        "controller_release_oid": controller["release_oid"],
        "controller_artifact_sha256": controller["artifact_sha256"],
        "candidate_receipt_digest": (
            candidate_receipt_digest
            or _task3_candidate_receipt_digest(candidate)
        ),
    }
    digest = promotion.candidate_integration_binding_digest(values)
    return promotion.CandidateIntegrationBinding(**values, binding_digest=digest)


def _bundle(tmp_path: Path, *slices: dict[str, object]):
    from agent.bestplan_contract import approval_digest

    repo = _repo(tmp_path / "repo")
    snapshot = _snapshot(repo)
    plan = _plan(repo, *slices)
    contract = _contract(snapshot, plan, repo)
    approved = approval_digest(plan.to_manifest(), contract)
    return repo, snapshot, plan, contract, approved


def _local_bundle(tmp_path: Path, *slices: dict[str, object]):
    from agent.bestplan_contract import (
        _command_from_dict,
        _controller_from_dict,
    )
    from agent.bestplan_local import build_local_go_contract

    repo, snapshot, plan, enrolled, _approved = _bundle(tmp_path, *slices)
    contract = build_local_go_contract(
        snapshot=snapshot,
        controller=_controller_from_dict(
            enrolled["controller"], "test local controller",
        ),
        commands=tuple(
            _command_from_dict(item, f"test local command[{index}]")
            for index, item in enumerate(enrolled["commands"])
        ),
        manifest_digest=_local_manifest_digest(plan),
        check_runtime_digest="e" * 64,
    )
    approved = _local_approval_digest(plan, contract)
    return repo, snapshot, plan, contract, approved


def _freeze_integration(
    promotion,
    tmp_path: Path,
    snapshot,
    plan,
    contract,
    approved: str,
    bindings,
    *,
    plan_id: str = "bp-integration",
    temp_root: Path | None = None,
    deadline: float | None = None,
    cancel_event: threading.Event | None = None,
    precreate_temp_root: bool = True,
):
    root = tmp_path / "integration-temp" if temp_root is None else Path(temp_root)
    if (
        precreate_temp_root
        and not root.exists()
        and not root.is_symlink()
    ):
        root.mkdir(mode=0o700)
    return promotion.freeze_integration(
        plan_id=plan_id,
        plan=plan,
        snapshot=snapshot,
        contract=contract,
        approval_digest=approved,
        candidates=tuple(bindings),
        temp_root=root,
        deadline=time.monotonic() + 30 if deadline is None else deadline,
        cancel_event=cancel_event,
    )


def _advance_main(
    repo: Path,
    changes: dict[str, str | None],
    message: str = "advance",
) -> str:
    for logical_path, content in changes.items():
        destination = repo / logical_path
        if content is None:
            destination.unlink()
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "refs/heads/main")


def _integration_refs(repo: Path) -> tuple[str, ...]:
    output = _git(
        repo,
        "for-each-ref",
        "--format=%(refname)",
        "refs/hermes-bestplan-integrations",
    )
    return tuple(output.splitlines()) if output else ()


def _commit_tree(repo: Path, tree_oid: str, *parents: str) -> str:
    args: list[str] = ["commit-tree", tree_oid]
    for parent in parents:
        args.extend(("-p", parent))
    return _git(
        repo,
        *args,
        input_text="tampered candidate\n",
        extra_env={
            "GIT_AUTHOR_NAME": "Promotion Test",
            "GIT_AUTHOR_EMAIL": "promotion@example.test",
            "GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z",
            "GIT_COMMITTER_NAME": "Promotion Test",
            "GIT_COMMITTER_EMAIL": "promotion@example.test",
            "GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z",
        },
    )


def _rebind(promotion, binding, **changes):
    values = {
        key: value
        for key, value in binding.__dict__.items()
        if key != "binding_digest"
    }
    values.update(changes)
    return promotion.CandidateIntegrationBinding(
        **values,
        binding_digest=promotion.candidate_integration_binding_digest(values),
    )


def test_freeze_applies_candidates_in_manifest_order_not_caller_order(tmp_path):
    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(
        tmp_path, _slice("one"), _slice("two")
    )
    one = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one"
    )
    two = _freeze_candidate(
        tmp_path,
        snapshot,
        plan_id="bp-integration",
        slice_id="two",
        manifest_index=1,
    )

    integration = _freeze_integration(
        promotion,
        tmp_path,
        snapshot,
        plan,
        contract,
        approved,
        (
            _binding(promotion, snapshot, contract, approved, two),
            _binding(promotion, snapshot, contract, approved, one),
        ),
    )

    assert isinstance(integration, promotion.FrozenIntegration)
    assert [item.manifest_index for item in integration.candidates] == [0, 1]
    assert [item.manifest_slice_id for item in integration.candidates] == ["one", "two"]
    assert [item.candidate_id for item in integration.candidates] == [
        _task5_safe_identifier("candidate", "bp-integration", 0, "one"),
        _task5_safe_identifier("candidate", "bp-integration", 1, "two"),
    ]
    assert _git(repo, "show", f"{integration.integration_oid}:one/result.txt") == "one-result"
    assert _git(repo, "show", f"{integration.integration_oid}:two/result.txt") == "two-result"
    assert integration.candidates[0].artifact_digests == (
        ("one/result.txt", hashlib.sha256(b"one-result\n").hexdigest()),
    )


def test_freeze_accepts_strict_local_go_contract(tmp_path):
    from agent.bestplan_local import local_go_contract_digest

    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _local_bundle(
        tmp_path, _slice("one"),
    )
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one",
    )

    integration = _freeze_integration(
        promotion,
        tmp_path,
        snapshot,
        plan,
        contract,
        approved,
        (_binding(promotion, snapshot, contract, approved, candidate),),
    )

    assert integration.contract_digest == local_go_contract_digest(contract)
    assert integration.approval_digest == approved
    assert integration.target_ref == "refs/heads/main"
    assert _git(repo, "show", f"{integration.integration_oid}:one/result.txt") == (
        "one-result"
    )


def test_freeze_rejects_local_manifest_not_bound_to_plan_before_integration(
    tmp_path,
):
    promotion = _promotion()
    repo, snapshot, plan, contract, _approved = _local_bundle(
        tmp_path, _slice("one"),
    )
    altered = copy.deepcopy(contract)
    altered["manifest_digest"] = "f" * 64
    approved = _raw_local_approval_digest(plan, altered)
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one",
    )
    binding = _binding(
        promotion, snapshot, altered, approved, candidate,
    )

    with pytest.raises(
        promotion.IntegrationValidationError,
        match=r"(?i)manifest|approval|contract",
    ):
        _freeze_integration(
            promotion,
            tmp_path,
            snapshot,
            plan,
            altered,
            approved,
            (binding,),
        )

    assert _integration_refs(repo) == ()


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("repository", "repository"),
        ("source", "source"),
        ("controller_binding", "candidate binding|controller"),
    ),
)
def test_freeze_binds_full_local_repository_source_and_controller_evidence(
    tmp_path, mutation, message,
):
    promotion = _promotion()
    repo, snapshot, plan, contract, _approved = _local_bundle(
        tmp_path, _slice("one"),
    )
    altered = copy.deepcopy(contract)
    if mutation == "repository":
        replacement = str((tmp_path / "different-worktree").resolve())
        altered["repository"]["workspace"] = replacement
        altered["repository"]["workspace_raw_b64"] = base64.b64encode(
            os.fsencode(replacement)
        ).decode("ascii")
    elif mutation == "source":
        altered["source"]["base_oid"] = "e" * 40
    approved = _raw_local_approval_digest(plan, altered)
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one",
    )
    binding = _binding(
        promotion, snapshot, altered, approved, candidate,
    )
    if mutation == "controller_binding":
        binding = _rebind(
            promotion, binding, controller_id="different-controller",
        )

    with pytest.raises(
        promotion.IntegrationValidationError,
        match=rf"(?i){message}",
    ):
        _freeze_integration(
            promotion,
            tmp_path,
            snapshot,
            plan,
            altered,
            approved,
            (binding,),
        )

    assert _integration_refs(repo) == ()


def test_freeze_still_rejects_legacy_candidate_only_contract(tmp_path):
    from agent.bestplan_contract import approval_digest, validate_execution_contract

    promotion = _promotion()
    repo, snapshot, plan, contract, _approved = _bundle(
        tmp_path, _slice("one"),
    )
    candidate_only = copy.deepcopy(contract)
    candidate_only["promotion_mode"] = "candidate_only"
    candidate_only = validate_execution_contract(candidate_only)
    approved = approval_digest(plan.to_manifest(), candidate_only)
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one",
    )
    binding = _binding(
        promotion, snapshot, candidate_only, approved, candidate,
    )

    with pytest.raises(
        promotion.IntegrationValidationError,
        match=r"(?i)auto_live|mode|approval",
    ):
        _freeze_integration(
            promotion,
            tmp_path,
            snapshot,
            plan,
            candidate_only,
            approved,
            (binding,),
        )

    assert _integration_refs(repo) == ()


def test_frozen_integration_retains_exact_task5_ids_and_task3_binding_evidence(
    tmp_path,
):
    promotion = _promotion()
    _repo_path, snapshot, plan, contract, approved = _bundle(
        tmp_path, _slice("one"),
    )
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one",
    )
    binding = _binding(promotion, snapshot, contract, approved, candidate)

    integration = _freeze_integration(
        promotion,
        tmp_path,
        snapshot,
        plan,
        contract,
        approved,
        (binding,),
    )

    assert binding.slice_id == _task5_safe_identifier(
        "slice", "bp-integration", 0, "one",
    )
    assert binding.candidate_id == _task5_safe_identifier(
        "candidate", "bp-integration", 0, "one",
    )
    assert binding.attempt_id == _task5_safe_identifier(
        "attempt", "bp-integration", 0, "one",
    )
    assert binding.candidate_receipt_digest != candidate.raw_receipt_sha256
    applied = integration.candidates[0]
    assert applied.manifest_slice_id == "one"
    assert applied.slice_id == binding.slice_id
    assert applied.candidate_id == binding.candidate_id
    assert applied.attempt_id == binding.attempt_id
    assert applied.policy_digest == binding.policy_digest
    assert applied.candidate_receipt_digest == binding.candidate_receipt_digest
    assert applied.binding_digest == binding.binding_digest


@pytest.mark.parametrize("identity", ("slice_id", "candidate_id", "attempt_id"))
def test_freeze_rejects_nonexact_task5_candidate_identity_before_integration(
    tmp_path, identity,
):
    from agent.bestplan_candidates import candidate_ref_name

    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(
        tmp_path, _slice("one"),
    )
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one",
    )
    binding = _binding(promotion, snapshot, contract, approved, candidate)
    changes = {}
    if identity == "slice_id":
        changes["slice_id"] = "one"
    elif identity == "candidate_id":
        changes["candidate_id"] = "candidate-substitute"
    else:
        changes["attempt_id"] = "attempt-substitute"
    effective_slice = changes.get("slice_id", binding.slice_id)
    effective_attempt = changes.get("attempt_id", binding.attempt_id)
    if identity in {"slice_id", "attempt_id"}:
        alternate_ref = candidate_ref_name(
            _task5_candidate_plan_id("bp-integration"),
            effective_slice,
            effective_attempt,
        )
        _git(repo, "update-ref", alternate_ref, candidate.commit_oid)
        changes["ref_name"] = alternate_ref
    tampered = _rebind(promotion, binding, **changes)

    with pytest.raises(
        promotion.IntegrationValidationError,
        match=r"(?i)task 5|slice|candidate|attempt|identity",
    ):
        _freeze_integration(
            promotion,
            tmp_path,
            snapshot,
            plan,
            contract,
            approved,
            (tampered,),
        )

    assert _integration_refs(repo) == ()


@pytest.mark.parametrize(
    ("field", "substitute"),
    (
        ("policy_digest", "e" * 64),
        ("candidate_receipt_digest", "f" * 64),
    ),
)
def test_retry_with_conflicting_candidate_evidence_is_not_adopted(
    tmp_path, field, substitute,
):
    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(
        tmp_path, _slice("one"),
    )
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one",
    )
    binding = _binding(promotion, snapshot, contract, approved, candidate)
    first = _freeze_integration(
        promotion,
        tmp_path,
        snapshot,
        plan,
        contract,
        approved,
        (binding,),
    )
    conflicting = _rebind(promotion, binding, **{field: substitute})

    with pytest.raises(
        promotion.IntegrationRefConflict,
        match=r"(?i)binding|receipt|policy|evidence|reference",
    ):
        _freeze_integration(
            promotion,
            tmp_path,
            snapshot,
            plan,
            contract,
            approved,
            (conflicting,),
        )

    assert _git(repo, "rev-parse", first.ref_name) == first.integration_oid


def test_final_written_index_tree_must_equal_the_composed_candidate_tree(
    tmp_path, monkeypatch,
):
    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(
        tmp_path, _slice("one"),
    )
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one",
    )
    binding = _binding(promotion, snapshot, contract, approved, candidate)
    real_write_tree = promotion._write_tree_from_map
    write_count = 0
    substituted = []

    def substitute_final_write_tree(repo_identity, tree, **kwargs):
        nonlocal write_count
        write_count += 1
        written = real_write_tree(repo_identity, tree, **kwargs)
        if write_count == 2:
            substituted.append(written)
            return snapshot.tree_oid
        return written

    monkeypatch.setattr(
        promotion, "_write_tree_from_map", substitute_final_write_tree,
    )

    with pytest.raises(
        promotion.IntegrationProofStale,
        match=r"(?i)written|composed|tree",
    ):
        _freeze_integration(
            promotion,
            tmp_path,
            snapshot,
            plan,
            contract,
            approved,
            (binding,),
        )

    assert substituted
    assert _integration_refs(repo) == ()


def test_integration_has_current_target_as_sole_parent_without_moving_main(tmp_path):
    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(tmp_path, _slice("one"))
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one"
    )
    target_oid = _advance_main(repo, {"target-only.txt": "target\n"})
    status_before = _git(repo, "status", "--porcelain=v1", "-z")

    integration = _freeze_integration(
        promotion,
        tmp_path,
        snapshot,
        plan,
        contract,
        approved,
        (_binding(promotion, snapshot, contract, approved, candidate),),
    )

    parents = _git(repo, "rev-list", "--parents", "-n", "1", integration.integration_oid).split()
    assert parents == [integration.integration_oid, target_oid]
    assert integration.target_oid == target_oid
    assert _git(repo, "rev-parse", "refs/heads/main") == target_oid
    assert _git(repo, "status", "--porcelain=v1", "-z") == status_before
    assert not (repo / "one" / "result.txt").exists()
    assert _git(repo, "show", f"{integration.integration_oid}:target-only.txt") == "target"


def test_current_target_overlap_blocks_integration_without_automatic_merge(tmp_path):
    promotion = _promotion()
    shared = _slice(
        "shared",
        allowed_paths=("shared.txt",),
        expected_artifacts=("shared.txt",),
    )
    repo, snapshot, plan, contract, approved = _bundle(tmp_path, shared)
    candidate = _freeze_candidate(
        tmp_path,
        snapshot,
        plan_id="bp-integration",
        slice_id="shared",
        changes={"shared.txt": "candidate\n"},
        allowed_paths=("shared.txt",),
        expected_artifacts=("shared.txt",),
    )
    target_oid = _advance_main(repo, {"shared.txt": "target\n"})

    with pytest.raises(promotion.IntegrationConflictError, match="conflict"):
        _freeze_integration(
            promotion,
            tmp_path,
            snapshot,
            plan,
            contract,
            approved,
            (_binding(promotion, snapshot, contract, approved, candidate),),
        )

    assert _git(repo, "rev-parse", "refs/heads/main") == target_oid
    assert _integration_refs(repo) == ()


def test_candidate_ref_is_reread_instead_of_trusting_the_binding(tmp_path):
    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(tmp_path, _slice("one"))
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one"
    )
    binding = _binding(promotion, snapshot, contract, approved, candidate)
    _git(repo, "update-ref", candidate.ref_name, snapshot.head_oid, candidate.commit_oid)

    with pytest.raises(promotion.IntegrationProofStale, match="candidate.*ref"):
        _freeze_integration(
            promotion, tmp_path, snapshot, plan, contract, approved, (binding,)
        )

    assert _integration_refs(repo) == ()


def test_candidate_commit_must_have_the_admitted_base_as_its_only_parent(tmp_path):
    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(tmp_path, _slice("one"))
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one"
    )
    orphan = _commit_tree(repo, candidate.tree_oid)
    _git(repo, "update-ref", candidate.ref_name, orphan, candidate.commit_oid)
    tampered = replace(candidate, commit_oid=orphan)

    with pytest.raises(promotion.IntegrationProofStale, match="parent|ancestry"):
        _freeze_integration(
            promotion,
            tmp_path,
            snapshot,
            plan,
            contract,
            approved,
            (_binding(promotion, snapshot, contract, approved, tampered),),
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("tree_oid", "BASE_TREE", "tree"),
        ("changed_paths", (b"one/not-the-change.txt",), "changed.*path"),
    ],
)
def test_candidate_tree_and_changed_paths_are_recomputed(
    tmp_path, field, value, message
):
    promotion = _promotion()
    _repo_path, snapshot, plan, contract, approved = _bundle(tmp_path, _slice("one"))
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one"
    )
    actual_value = snapshot.tree_oid if value == "BASE_TREE" else value
    tampered = replace(candidate, **{field: actual_value})

    with pytest.raises(promotion.IntegrationProofStale, match=message):
        _freeze_integration(
            promotion,
            tmp_path,
            snapshot,
            plan,
            contract,
            approved,
            (_binding(promotion, snapshot, contract, approved, tampered),),
        )


def test_recomputed_candidate_delta_must_stay_inside_manifest_lease(tmp_path):
    promotion = _promotion()
    _repo_path, snapshot, plan, contract, approved = _bundle(tmp_path, _slice("one"))
    candidate = _freeze_candidate(
        tmp_path,
        snapshot,
        plan_id="bp-integration",
        slice_id="one",
        changes={
            "one/result.txt": "one-result\n",
            "outside/change.txt": "outside\n",
        },
        allowed_paths=("one/", "outside/"),
    )

    with pytest.raises(promotion.IntegrationValidationError, match="lease"):
        _freeze_integration(
            promotion,
            tmp_path,
            snapshot,
            plan,
            contract,
            approved,
            (_binding(promotion, snapshot, contract, approved, candidate),),
        )


def test_manifest_expected_artifact_is_revalidated_from_candidate_tree(tmp_path):
    promotion = _promotion()
    _repo_path, snapshot, plan, contract, approved = _bundle(tmp_path, _slice("one"))
    candidate = _freeze_candidate(
        tmp_path,
        snapshot,
        plan_id="bp-integration",
        slice_id="one",
        changes={"one/other.txt": "other\n"},
        expected_artifacts=("one/base.txt",),
    )

    with pytest.raises(promotion.IntegrationValidationError, match="artifact"):
        _freeze_integration(
            promotion,
            tmp_path,
            snapshot,
            plan,
            contract,
            approved,
            (_binding(promotion, snapshot, contract, approved, candidate),),
        )


def test_candidate_cannot_replace_an_enrollment_pinned_checker_input(tmp_path):
    promotion = _promotion()
    gate_slice = _slice(
        "gate",
        allowed_paths=("config/",),
        expected_artifacts=("config/gate.txt",),
    )
    _repo_path, snapshot, plan, contract, approved = _bundle(tmp_path, gate_slice)
    candidate = _freeze_candidate(
        tmp_path,
        snapshot,
        plan_id="bp-integration",
        slice_id="gate",
        changes={"config/gate.txt": "candidate-controlled gate\n"},
        allowed_paths=("config/",),
        expected_artifacts=("config/gate.txt",),
    )

    with pytest.raises(promotion.IntegrationValidationError, match="pinned.*input"):
        _freeze_integration(
            promotion,
            tmp_path,
            snapshot,
            plan,
            contract,
            approved,
            (_binding(promotion, snapshot, contract, approved, candidate),),
        )


def test_manifest_with_dependencies_is_rejected_even_with_complete_candidates(tmp_path):
    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(
        tmp_path,
        _slice("one"),
        _slice("two", depends_on=("one",)),
    )
    one = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one"
    )
    two = _freeze_candidate(
        tmp_path,
        snapshot,
        plan_id="bp-integration",
        slice_id="two",
        manifest_index=1,
    )

    with pytest.raises(promotion.IntegrationValidationError, match="depend"):
        _freeze_integration(
            promotion,
            tmp_path,
            snapshot,
            plan,
            contract,
            approved,
            (
                _binding(promotion, snapshot, contract, approved, one),
                _binding(promotion, snapshot, contract, approved, two),
            ),
        )

    assert _integration_refs(repo) == ()


def test_exact_retry_adopts_the_same_deterministic_integration_commit(tmp_path):
    promotion = _promotion()
    _repo_path, snapshot, plan, contract, approved = _bundle(tmp_path, _slice("one"))
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one"
    )
    binding = _binding(promotion, snapshot, contract, approved, candidate)

    first = _freeze_integration(
        promotion, tmp_path, snapshot, plan, contract, approved, (binding,)
    )
    second = _freeze_integration(
        promotion, tmp_path, snapshot, plan, contract, approved, (binding,)
    )

    assert second == first
    assert second.integration_oid == first.integration_oid
    assert second.receipt_digest == first.receipt_digest


def test_owned_integration_ref_pointing_elsewhere_is_a_conflict(tmp_path):
    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(tmp_path, _slice("one"))
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one"
    )
    binding = _binding(promotion, snapshot, contract, approved, candidate)
    integration = _freeze_integration(
        promotion, tmp_path, snapshot, plan, contract, approved, (binding,)
    )
    _git(
        repo,
        "update-ref",
        integration.ref_name,
        snapshot.head_oid,
        integration.integration_oid,
    )

    with pytest.raises(promotion.IntegrationRefConflict, match="reference|ref"):
        _freeze_integration(
            promotion, tmp_path, snapshot, plan, contract, approved, (binding,)
        )


def test_target_change_invalidates_a_previously_frozen_integration(tmp_path):
    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(tmp_path, _slice("one"))
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one"
    )
    binding = _binding(promotion, snapshot, contract, approved, candidate)
    first = _freeze_integration(
        promotion, tmp_path, snapshot, plan, contract, approved, (binding,)
    )
    moved_target = _advance_main(repo, {"later.txt": "later\n"}, "move target")

    with pytest.raises(promotion.IntegrationProofStale, match="target"):
        _freeze_integration(
            promotion, tmp_path, snapshot, plan, contract, approved, (binding,)
        )

    assert moved_target != first.target_oid
    assert _git(repo, "rev-parse", first.ref_name) == first.integration_oid


def test_current_target_must_descend_from_the_admitted_base(tmp_path):
    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(tmp_path, _slice("one"))
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one"
    )
    unrelated = _commit_tree(repo, snapshot.tree_oid)
    _git(repo, "update-ref", "refs/heads/main", unrelated, snapshot.head_oid)

    with pytest.raises(promotion.IntegrationProofStale, match="target.*ancestry|descend"):
        _freeze_integration(
            promotion,
            tmp_path,
            snapshot,
            plan,
            contract,
            approved,
            (_binding(promotion, snapshot, contract, approved, candidate),),
        )


def test_materialization_exports_exact_integration_without_git_metadata(tmp_path):
    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(tmp_path, _slice("one"))
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one"
    )
    integration = _freeze_integration(
        promotion,
        tmp_path,
        snapshot,
        plan,
        contract,
        approved,
        (_binding(promotion, snapshot, contract, approved, candidate),),
    )
    main_before = _git(repo, "rev-parse", "refs/heads/main")
    status_before = _git(repo, "status", "--porcelain=v1", "-z")
    destination = tmp_path / "materialized"

    promotion.materialize_integration_tree(
        snapshot=snapshot,
        integration=integration,
        destination=destination,
        deadline=time.monotonic() + 30,
    )

    assert destination.is_dir() and not destination.is_symlink()
    assert (destination / "one" / "result.txt").read_bytes() == b"one-result\n"
    assert not any(path.name == ".git" for path in destination.rglob("*"))
    assert not (destination / ".git").exists()
    assert _git(repo, "rev-parse", "refs/heads/main") == main_before
    assert _git(repo, "status", "--porcelain=v1", "-z") == status_before


def test_fd_native_materialization_survives_admitted_parent_ancestor_swap(
    tmp_path, monkeypatch,
):
    promotion = _promotion()
    _repo, snapshot, plan, contract, approved = _bundle(
        tmp_path, _slice("one"),
    )
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one",
    )
    integration = _freeze_integration(
        promotion,
        tmp_path,
        snapshot,
        plan,
        contract,
        approved,
        (_binding(promotion, snapshot, contract, approved, candidate),),
    )
    host_ancestor = tmp_path / "admitted-host-ancestor"
    admitted_parent = host_ancestor / "owned-attempt"
    admitted_parent.mkdir(parents=True, mode=0o700)
    replacement = tmp_path / "replacement-host-ancestor"
    replacement_parent = replacement / "owned-attempt"
    replacement_parent.mkdir(parents=True, mode=0o700)
    replacement_sentinel = replacement_parent / "sentinel.txt"
    replacement_sentinel.write_text("replacement sentinel\n", encoding="utf-8")
    displaced_ancestor = tmp_path / "displaced-host-ancestor"
    cancellation = threading.Event()
    absolute_deadline = time.monotonic() + 30
    observed_control = []
    real_materialize_blobs = promotion.source_boundary._materialize_blobs

    def capture_blob_control(
        *args,
        deadline=None,
        cancel_event=None,
        **kwargs,
    ):
        observed_control.append((deadline, cancel_event))
        supported = inspect.signature(real_materialize_blobs).parameters
        forwarded = {
            key: value
            for key, value in {
                **kwargs,
                "deadline": deadline,
                "cancel_event": cancel_event,
            }.items()
            if key in supported
        }
        return real_materialize_blobs(*args, **forwarded)

    monkeypatch.setattr(
        promotion.source_boundary,
        "_materialize_blobs",
        capture_blob_control,
    )

    def forbidden_parent_path_reopen(*_args, **_kwargs):
        raise AssertionError(
            "fd-native materialization reopened its admitted parent pathname"
        )

    monkeypatch.setattr(
        promotion.source_boundary,
        "_prepare_destination",
        forbidden_parent_path_reopen,
    )
    monkeypatch.setattr(
        promotion.source_boundary,
        "_verify_destination_parent",
        forbidden_parent_path_reopen,
    )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(admitted_parent, flags)
    opened = os.fstat(parent_fd)
    parent_identity = (
        opened.st_dev,
        opened.st_ino,
        opened.st_mode,
        opened.st_uid,
    )
    host_ancestor.rename(displaced_ancestor)
    host_ancestor.symlink_to(replacement, target_is_directory=True)

    try:
        operation = promotion._materialize_integration_tree_at_owned_parent
        parameters = inspect.signature(operation).parameters
        assert "destination" not in parameters
        assert {
            "parent_fd",
            "parent_identity",
            "destination_leaf",
            "deadline",
            "cancel_event",
        } <= set(parameters)
        operation(
            snapshot=snapshot,
            integration=integration,
            parent_fd=parent_fd,
            parent_identity=parent_identity,
            destination_leaf=b"integration",
            deadline=absolute_deadline,
            cancel_event=cancellation,
        )
    finally:
        os.close(parent_fd)

    materialized = displaced_ancestor / "owned-attempt" / "integration"
    assert (materialized / "one" / "result.txt").read_bytes() == b"one-result\n"
    assert not any(path.name == ".git" for path in materialized.rglob("*"))
    assert not (replacement_parent / "integration").exists()
    assert replacement_sentinel.read_text(encoding="utf-8") == (
        "replacement sentinel\n"
    )
    assert tuple(replacement_parent.iterdir()) == (replacement_sentinel,)
    assert observed_control == [(absolute_deadline, cancellation)]


def test_materialization_rejects_stale_target_before_creating_output(tmp_path):
    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(
        tmp_path, _slice("one"),
    )
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one",
    )
    integration = _freeze_integration(
        promotion,
        tmp_path,
        snapshot,
        plan,
        contract,
        approved,
        (_binding(promotion, snapshot, contract, approved, candidate),),
    )
    export_root = tmp_path / "exports"
    export_root.mkdir(mode=0o700)
    destination = export_root / "materialized"
    children_before = tuple(export_root.iterdir())
    moved_target = _advance_main(
        repo,
        {"target-moved-before-export.txt": "moved\n"},
        "move target before materialization",
    )

    with pytest.raises(promotion.IntegrationProofStale, match="target|ref"):
        promotion.materialize_integration_tree(
            snapshot=snapshot,
            integration=integration,
            destination=destination,
            deadline=time.monotonic() + 30,
        )

    assert moved_target != integration.target_oid
    assert not destination.exists()
    assert tuple(export_root.iterdir()) == children_before


def test_materialization_quarantines_output_if_target_changes_during_publication(
    tmp_path, monkeypatch,
):
    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(
        tmp_path, _slice("one"),
    )
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one",
    )
    integration = _freeze_integration(
        promotion,
        tmp_path,
        snapshot,
        plan,
        contract,
        approved,
        (_binding(promotion, snapshot, contract, approved, candidate),),
    )
    export_root = tmp_path / "exports"
    export_root.mkdir(mode=0o700)
    destination = export_root / "materialized"
    absolute_deadline = time.monotonic() + 30
    real_publish = promotion.source_boundary._publish_staging_no_replace
    real_quarantine = promotion.source_boundary._quarantine_owned_published
    moved_targets = []
    cleanup_deadlines = []

    def publish_then_move_target(prepared, **kwargs):
        real_publish(prepared, **kwargs)
        moved_targets.append(_advance_main(
            repo,
            {"target-moved-during-export.txt": "moved\n"},
            "move target during materialization",
        ))

    def capture_quarantine_deadline(*args, deadline=None, **kwargs):
        cleanup_deadlines.append(deadline)
        if "deadline" in inspect.signature(real_quarantine).parameters:
            kwargs["deadline"] = deadline
        return real_quarantine(*args, **kwargs)

    monkeypatch.setattr(
        promotion.source_boundary,
        "_publish_staging_no_replace",
        publish_then_move_target,
    )
    monkeypatch.setattr(
        promotion.source_boundary,
        "_quarantine_owned_published",
        capture_quarantine_deadline,
    )

    with pytest.raises(promotion.IntegrationProofStale, match="target|ref"):
        promotion.materialize_integration_tree(
            snapshot=snapshot,
            integration=integration,
            destination=destination,
            deadline=absolute_deadline,
        )

    assert moved_targets and moved_targets[-1] != integration.target_oid
    assert not destination.exists()
    assert not any(not item.name.startswith(".") for item in export_root.iterdir())
    assert cleanup_deadlines == [absolute_deadline]


def test_materialization_cancellation_after_blob_write_never_publishes_output(
    tmp_path, monkeypatch,
):
    promotion = _promotion()
    _repo_path, snapshot, plan, contract, approved = _bundle(
        tmp_path, _slice("one"),
    )
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one",
    )
    integration = _freeze_integration(
        promotion,
        tmp_path,
        snapshot,
        plan,
        contract,
        approved,
        (_binding(promotion, snapshot, contract, approved, candidate),),
    )
    export_root = tmp_path / "exports"
    export_root.mkdir(mode=0o700)
    destination = export_root / "materialized"
    cancellation = threading.Event()
    absolute_deadline = time.monotonic() + 30
    real_materialize = promotion.source_boundary._materialize_blobs
    real_cleanup = promotion.source_boundary._cleanup_owned_staging
    cleanup_deadlines = []
    publish_calls = []

    def cancel_after_blobs(*args, **kwargs):
        supported = inspect.signature(real_materialize).parameters
        filtered = {
            key: value for key, value in kwargs.items() if key in supported
        }
        result = real_materialize(*args, **filtered)
        cancellation.set()
        return result

    def capture_cleanup_deadline(*args, deadline=None, **kwargs):
        cleanup_deadlines.append(deadline)
        if "deadline" in inspect.signature(real_cleanup).parameters:
            kwargs["deadline"] = deadline
        return real_cleanup(*args, **kwargs)

    monkeypatch.setattr(
        promotion.source_boundary,
        "_materialize_blobs",
        cancel_after_blobs,
    )
    monkeypatch.setattr(
        promotion.source_boundary,
        "_cleanup_owned_staging",
        capture_cleanup_deadline,
    )
    monkeypatch.setattr(
        promotion.source_boundary,
        "_publish_staging_no_replace",
        lambda *args, **kwargs: publish_calls.append((args, kwargs)),
    )

    with pytest.raises(promotion.PromotionError, match="cancel"):
        promotion.materialize_integration_tree(
            snapshot=snapshot,
            integration=integration,
            destination=destination,
            deadline=absolute_deadline,
            cancel_event=cancellation,
        )

    assert cancellation.is_set()
    assert publish_calls == []
    assert cleanup_deadlines == [absolute_deadline]
    assert not destination.exists()
    assert tuple(export_root.iterdir()) == ()


def test_materialization_threads_same_control_through_blob_and_cleanup_work(
    tmp_path, monkeypatch,
):
    promotion = _promotion()
    _repo_path, snapshot, plan, contract, approved = _bundle(
        tmp_path, _slice("one"),
    )
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one",
    )
    integration = _freeze_integration(
        promotion,
        tmp_path,
        snapshot,
        plan,
        contract,
        approved,
        (_binding(promotion, snapshot, contract, approved, candidate),),
    )
    export_root = tmp_path / "controlled-exports"
    export_root.mkdir(mode=0o700)
    destination = export_root / "materialized"
    cancellation = threading.Event()
    absolute_deadline = time.monotonic() + 30
    observed = {"blob": [], "cleanup": []}
    publish_calls = []

    def cancel_during_blob_work(
        *args,
        deadline=None,
        cancel_event=None,
        **kwargs,
    ):
        observed["blob"].append((deadline, cancel_event))
        cancellation.set()
        raise promotion.PromotionError("integration cancelled during blob work")

    def retain_owned_staging(
        prepared,
        *,
        deadline=None,
        cancel_event=None,
    ):
        observed["cleanup"].append((deadline, cancel_event))

    monkeypatch.setattr(
        promotion.source_boundary,
        "_materialize_blobs",
        cancel_during_blob_work,
    )
    monkeypatch.setattr(
        promotion.source_boundary,
        "_cleanup_owned_staging",
        retain_owned_staging,
    )
    monkeypatch.setattr(
        promotion.source_boundary,
        "_publish_staging_no_replace",
        lambda *args, **kwargs: publish_calls.append((args, kwargs)),
    )

    with pytest.raises(promotion.PromotionError, match="cancel"):
        promotion.materialize_integration_tree(
            snapshot=snapshot,
            integration=integration,
            destination=destination,
            deadline=absolute_deadline,
            cancel_event=cancellation,
        )

    assert observed == {
        "blob": [(absolute_deadline, cancellation)],
        "cleanup": [(absolute_deadline, cancellation)],
    }
    assert publish_calls == []
    assert not destination.exists()
    retained = tuple(export_root.iterdir())
    assert retained and all(item.name.startswith(".") for item in retained)


def test_materialization_real_cleanup_uses_caller_control_without_fresh_budget(
    tmp_path, monkeypatch,
):
    promotion = _promotion()
    _repo_path, snapshot, plan, contract, approved = _bundle(
        tmp_path, _slice("one"),
    )
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one",
    )
    integration = _freeze_integration(
        promotion,
        tmp_path,
        snapshot,
        plan,
        contract,
        approved,
        (_binding(promotion, snapshot, contract, approved, candidate),),
    )
    export_root = tmp_path / "bounded-cleanup-exports"
    export_root.mkdir(mode=0o700)
    destination = export_root / "materialized"
    cancellation = threading.Event()
    absolute_deadline = time.monotonic() + 30
    observed = []
    source = promotion.source_boundary

    def fail_blob_work(*args, **kwargs):
        raise source.SourceBoundaryError("forced materialization failure")

    def stop_cleanup_walk(
        directory_fd,
        *,
        deadline,
        cancel_event=None,
    ):
        observed.append((deadline, cancel_event))
        raise source.SourceBoundaryError("retain bounded cleanup evidence")

    monkeypatch.setattr(source, "_materialize_blobs", fail_blob_work)
    monkeypatch.setattr(source, "_remove_owned_tree_contents", stop_cleanup_walk)

    with pytest.raises(promotion.PromotionError, match="cleanup|quarantined"):
        promotion.materialize_integration_tree(
            snapshot=snapshot,
            integration=integration,
            destination=destination,
            deadline=absolute_deadline,
            cancel_event=cancellation,
        )

    assert observed == [(absolute_deadline, cancellation)]
    assert not destination.exists()
    retained = tuple(export_root.iterdir())
    assert retained and all(item.name.startswith(".") for item in retained)


@pytest.mark.parametrize("entrypoint", ("freeze", "materialize"))
def test_public_promotion_rejects_invalid_cancel_event_before_entry(
    tmp_path, monkeypatch, entrypoint,
):
    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(
        tmp_path, _slice("one"),
    )
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one",
    )
    binding = _binding(promotion, snapshot, contract, approved, candidate)
    integration = None
    if entrypoint == "materialize":
        integration = _freeze_integration(
            promotion,
            tmp_path,
            snapshot,
            plan,
            contract,
            approved,
            (binding,),
        )
    refs_before = _integration_refs(repo)
    destination = tmp_path / "invalid-cancel-output"
    entry_calls = []

    def forbidden_repo_entry(*args, **kwargs):
        entry_calls.append((args, kwargs))
        raise AssertionError("invalid cancel event crossed the public entry gate")

    monkeypatch.setattr(promotion, "_assert_repo_identity", forbidden_repo_entry)

    with pytest.raises(
        promotion.IntegrationValidationError,
        match=r"(?i)cancel",
    ):
        if entrypoint == "freeze":
            _freeze_integration(
                promotion,
                tmp_path,
                snapshot,
                plan,
                contract,
                approved,
                (binding,),
                cancel_event=object(),
            )
        else:
            promotion.materialize_integration_tree(
                snapshot=snapshot,
                integration=integration,
                destination=destination,
                deadline=time.monotonic() + 30,
                cancel_event=object(),
            )

    assert entry_calls == []
    assert not destination.exists()
    assert _integration_refs(repo) == refs_before


@pytest.mark.parametrize("entrypoint", ("freeze", "materialize"))
def test_public_promotion_rejects_precancelled_operation_before_entry(
    tmp_path, monkeypatch, entrypoint,
):
    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(
        tmp_path, _slice("one"),
    )
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one",
    )
    binding = _binding(promotion, snapshot, contract, approved, candidate)
    integration = None
    if entrypoint == "materialize":
        integration = _freeze_integration(
            promotion,
            tmp_path,
            snapshot,
            plan,
            contract,
            approved,
            (binding,),
        )
    refs_before = _integration_refs(repo)
    destination = tmp_path / "precancelled-output"
    cancellation = threading.Event()
    cancellation.set()
    entry_calls = []

    def forbidden_repo_entry(*args, **kwargs):
        entry_calls.append((args, kwargs))
        raise AssertionError("precancelled operation crossed the public entry gate")

    monkeypatch.setattr(promotion, "_assert_repo_identity", forbidden_repo_entry)

    with pytest.raises(promotion.PromotionError, match="cancel"):
        if entrypoint == "freeze":
            _freeze_integration(
                promotion,
                tmp_path,
                snapshot,
                plan,
                contract,
                approved,
                (binding,),
                cancel_event=cancellation,
            )
        else:
            promotion.materialize_integration_tree(
                snapshot=snapshot,
                integration=integration,
                destination=destination,
                deadline=time.monotonic() + 30,
                cancel_event=cancellation,
            )

    assert entry_calls == []
    assert not destination.exists()
    assert _integration_refs(repo) == refs_before


@pytest.mark.parametrize("boundary", ("deadline", "cancel"))
def test_composed_tree_alias_loop_observes_deadline_and_cancellation(
    boundary,
):
    promotion = _promotion()
    cancellation = threading.Event()
    deadline = time.monotonic() + 30
    if boundary == "deadline":
        deadline = time.monotonic() - 1
    else:
        cancellation.set()
    iterated = []

    class _ExplosiveTree(dict):
        def __iter__(self):
            iterated.append(True)
            raise AssertionError("bounded composed-tree loop started after stop")

    started = time.perf_counter()
    with pytest.raises(promotion.PromotionError, match="deadline|cancel"):
        promotion._assert_composed_tree_has_no_component_aliases(
            _ExplosiveTree(),
            deadline=deadline,
            cancel_event=cancellation,
        )
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5
    assert iterated == []


def test_freeze_propagates_deadline_and_cancel_to_temp_root_validation(
    tmp_path, monkeypatch,
):
    promotion = _promotion()
    _repo_path, snapshot, plan, contract, approved = _bundle(
        tmp_path, _slice("one"),
    )
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one",
    )
    binding = _binding(promotion, snapshot, contract, approved, candidate)
    cancellation = threading.Event()
    absolute_deadline = time.monotonic() + 30
    observed = []
    real_prepare = promotion._prepare_temp_root

    def capture_prepare(value, *, repo, deadline=None, cancel_event=None):
        observed.append((deadline, cancel_event))
        kwargs = {"repo": repo}
        parameters = inspect.signature(real_prepare).parameters
        if "deadline" in parameters:
            kwargs["deadline"] = deadline
        if "cancel_event" in parameters:
            kwargs["cancel_event"] = cancel_event
        return real_prepare(value, **kwargs)

    monkeypatch.setattr(promotion, "_prepare_temp_root", capture_prepare)

    _freeze_integration(
        promotion,
        tmp_path,
        snapshot,
        plan,
        contract,
        approved,
        (binding,),
        deadline=absolute_deadline,
        cancel_event=cancellation,
    )

    assert observed == [(absolute_deadline, cancellation)]


def test_freeze_does_not_start_unbounded_cleanup_after_deadline(
    tmp_path, monkeypatch,
):
    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(
        tmp_path, _slice("one"),
    )
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one",
    )
    binding = _binding(promotion, snapshot, contract, approved, candidate)
    temp_root = tmp_path / "integration-temp"
    temp_root.mkdir(mode=0o700)
    real_monotonic = time.monotonic
    real_scandir = os.scandir
    absolute_deadline = real_monotonic() + 30
    expired = False
    expired_at = None
    raw_cleanup_calls = []

    def controlled_monotonic():
        return absolute_deadline + 1 if expired else real_monotonic()

    def expire_inside_owned_attempt(*args, **kwargs):
        nonlocal expired, expired_at
        expired = True
        expired_at = time.perf_counter()
        raise promotion.PromotionError("integration deadline expired")

    def forbidden_raw_cleanup(path, *args, **kwargs):
        if expired:
            raw_cleanup_calls.append((path, args, kwargs))
            raise AssertionError(
                "expired attempt entered unbounded recursive cleanup"
            )
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(promotion.time, "monotonic", controlled_monotonic)
    monkeypatch.setattr(
        promotion, "_write_tree_from_delta", expire_inside_owned_attempt,
    )
    monkeypatch.setattr(promotion.os, "scandir", forbidden_raw_cleanup)

    with pytest.raises(promotion.PromotionError, match="deadline|cleanup"):
        _freeze_integration(
            promotion,
            tmp_path,
            snapshot,
            plan,
            contract,
            approved,
            (binding,),
            temp_root=temp_root,
            deadline=absolute_deadline,
        )
    finished = time.perf_counter()

    assert expired is True
    assert expired_at is not None
    assert finished - expired_at < 0.5
    assert raw_cleanup_calls == []
    assert _integration_refs(repo) == ()


@pytest.mark.parametrize("control_kind", ("deadline", "cancel"))
def test_owned_temp_cleanup_observes_control_during_walk_without_rmtree(
    tmp_path, monkeypatch, control_kind,
):
    promotion = _promotion()
    _repo, snapshot, _plan_value, _contract_value, _approved = _bundle(
        tmp_path, _slice("one"),
    )
    temp_root = tmp_path / "owned-cleanup-root"
    temp_root.mkdir(mode=0o700)
    absolute_deadline = time.monotonic() + 30
    prepared = promotion._prepare_temp_root(
        temp_root,
        repo=snapshot.repo,
        deadline=absolute_deadline,
        cancel_event=None,
    )
    attempt = promotion._create_owned_temp_attempt(
        prepared,
        deadline=absolute_deadline,
        cancel_event=None,
    )
    nested = attempt.path / "nested"
    nested.mkdir()
    sentinel = nested / "retain.txt"
    sentinel.write_text("retain\n", encoding="utf-8")
    raw_rmtree_calls = []
    control_observations = []

    def forbidden_rmtree(*args, **kwargs):
        raw_rmtree_calls.append((args, kwargs))
        raise AssertionError("bounded temp cleanup delegated to raw rmtree")

    monkeypatch.setattr(shutil, "rmtree", forbidden_rmtree)
    if control_kind == "deadline":
        clock_calls = 0

        def expire_after_entry():
            nonlocal clock_calls
            clock_calls += 1
            control_observations.append(clock_calls)
            return 99.0 if clock_calls == 1 else 101.0

        monkeypatch.setattr(promotion.time, "monotonic", expire_after_entry)
        cleanup_kwargs = {"deadline": 100.0}
    else:
        class CancelDuringCleanup(threading.Event):
            def is_set(self):
                control_observations.append(len(control_observations) + 1)
                return len(control_observations) >= 2

        cancellation = CancelDuringCleanup()
        cleanup_kwargs = {
            "deadline": absolute_deadline,
            "cancel_event": cancellation,
        }

    try:
        if control_kind == "cancel":
            assert "cancel_event" in inspect.signature(
                promotion._cleanup_owned_temp_attempt
            ).parameters
        try:
            promotion._cleanup_owned_temp_attempt(
                attempt,
                **cleanup_kwargs,
            )
        except promotion.PromotionError:
            pass
    finally:
        os.close(prepared.descriptor)

    assert len(control_observations) >= 2
    assert raw_rmtree_calls == []
    assert sentinel.is_file()


@pytest.mark.parametrize("target_content", ("target replacement\n", None))
def test_final_integration_revalidates_unchanged_expected_artifact_after_target_change(
    tmp_path, target_content,
):
    promotion = _promotion()
    artifact_slice = _slice(
        "one",
        allowed_paths=("one/",),
        expected_artifacts=("one/base.txt",),
    )
    repo, snapshot, plan, contract, approved = _bundle(tmp_path, artifact_slice)
    candidate = _freeze_candidate(
        tmp_path,
        snapshot,
        plan_id="bp-integration",
        slice_id="one",
        changes={"one/result.txt": "candidate result\n"},
        allowed_paths=("one/",),
        expected_artifacts=("one/base.txt",),
    )
    target_oid = _advance_main(
        repo,
        {"one/base.txt": target_content},
        "change unchanged expected artifact",
    )

    with pytest.raises(promotion.PromotionError, match="artifact|conflict"):
        _freeze_integration(
            promotion,
            tmp_path,
            snapshot,
            plan,
            contract,
            approved,
            (_binding(promotion, snapshot, contract, approved, candidate),),
        )

    assert _git(repo, "rev-parse", "refs/heads/main") == target_oid
    assert _integration_refs(repo) == ()


def test_casefolded_directory_alias_between_target_and_candidate_is_conflict(tmp_path):
    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(
        tmp_path,
        _slice(
            "alias",
            allowed_paths=("foo/",),
            expected_artifacts=("foo/result.txt",),
        ),
    )
    candidate = _freeze_candidate(
        tmp_path,
        snapshot,
        plan_id="bp-integration",
        slice_id="alias",
        changes={"foo/result.txt": "candidate\n"},
        allowed_paths=("foo/",),
        expected_artifacts=("foo/result.txt",),
    )
    target_oid = _advance_main(
        repo,
        {"Foo/target.txt": "target\n"},
        "add casefolded target directory",
    )

    with pytest.raises(promotion.IntegrationConflictError, match="alias|conflict"):
        _freeze_integration(
            promotion,
            tmp_path,
            snapshot,
            plan,
            contract,
            approved,
            (_binding(promotion, snapshot, contract, approved, candidate),),
        )

    assert _git(repo, "rev-parse", "refs/heads/main") == target_oid
    assert _integration_refs(repo) == ()


@pytest.mark.parametrize("deadline", (float("nan"), float("inf")))
def test_freeze_rejects_nonfinite_deadline_before_any_side_effect(
    tmp_path, monkeypatch, deadline,
):
    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(tmp_path, _slice("one"))
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one"
    )
    binding = _binding(promotion, snapshot, contract, approved, candidate)
    temp_root = tmp_path / "must-not-exist"

    def forbidden_popen(*_args, **_kwargs):
        raise AssertionError("nonfinite deadline reached process launch")

    with monkeypatch.context() as scoped:
        scoped.setattr(promotion.subprocess, "Popen", forbidden_popen)
        with pytest.raises(promotion.IntegrationValidationError, match="deadline"):
            promotion.freeze_integration(
                plan_id="bp-integration",
                plan=plan,
                snapshot=snapshot,
                contract=contract,
                approval_digest=approved,
                candidates=(binding,),
                temp_root=temp_root,
                deadline=deadline,
            )

    assert not temp_root.exists()
    assert _integration_refs(repo) == ()


@pytest.mark.parametrize("deadline", (float("nan"), float("inf")))
def test_materialize_rejects_nonfinite_deadline_before_any_side_effect(
    tmp_path, monkeypatch, deadline,
):
    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(tmp_path, _slice("one"))
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one"
    )
    integration = _freeze_integration(
        promotion,
        tmp_path,
        snapshot,
        plan,
        contract,
        approved,
        (_binding(promotion, snapshot, contract, approved, candidate),),
    )
    destination = tmp_path / "must-not-materialize"
    refs_before = _integration_refs(repo)

    def forbidden_popen(*_args, **_kwargs):
        raise AssertionError("nonfinite deadline reached process launch")

    with monkeypatch.context() as scoped:
        scoped.setattr(promotion.subprocess, "Popen", forbidden_popen)
        with pytest.raises(promotion.IntegrationValidationError, match="deadline"):
            promotion.materialize_integration_tree(
                snapshot=snapshot,
                integration=integration,
                destination=destination,
                deadline=deadline,
            )

    assert not destination.exists()
    assert _integration_refs(repo) == refs_before


@pytest.mark.parametrize("alias_kind", ("direct", "symlink"))
def test_temp_root_alias_of_git_state_is_rejected_before_creation(
    tmp_path, alias_kind,
):
    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(tmp_path, _slice("one"))
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one"
    )
    binding = _binding(promotion, snapshot, contract, approved, candidate)
    protected_root = repo / ".git"
    if alias_kind == "direct":
        temp_root = protected_root
    else:
        alias = tmp_path / "git-state-alias"
        alias.symlink_to(protected_root, target_is_directory=True)
        temp_root = alias
    git_children_before = {item.name for item in (repo / ".git").iterdir()}

    with pytest.raises(promotion.IntegrationValidationError, match="temp root|repository"):
        promotion.freeze_integration(
            plan_id="bp-integration",
            plan=plan,
            snapshot=snapshot,
            contract=contract,
            approval_digest=approved,
            candidates=(binding,),
            temp_root=temp_root,
            deadline=time.monotonic() + 30,
        )

    assert {item.name for item in (repo / ".git").iterdir()} == git_children_before
    assert _integration_refs(repo) == ()


def test_temp_root_must_be_precreated_without_any_forbidden_mkdir(
    tmp_path, monkeypatch,
):
    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(
        tmp_path, _slice("one"),
    )
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one",
    )
    binding = _binding(promotion, snapshot, contract, approved, candidate)
    temp_root = tmp_path / "missing-integration-temp"
    forbidden_mkdir = []
    real_mkdir = Path.mkdir

    def reject_temp_mkdir(path, *args, **kwargs):
        candidate_path = Path(path).absolute()
        if candidate_path == temp_root or temp_root in candidate_path.parents:
            forbidden_mkdir.append(candidate_path)
            raise AssertionError("promotion attempted to create its host temp root")
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", reject_temp_mkdir)

    with pytest.raises(
        promotion.IntegrationValidationError,
        match=r"(?i)temp root|exist",
    ):
        _freeze_integration(
            promotion,
            tmp_path,
            snapshot,
            plan,
            contract,
            approved,
            (binding,),
            temp_root=temp_root,
            precreate_temp_root=False,
        )

    assert forbidden_mkdir == []
    assert not temp_root.exists()
    assert _integration_refs(repo) == ()


@pytest.mark.parametrize("mode", (0o755, 0o750, 0o700 | 0o020))
def test_temp_root_must_be_private_before_attempt_creation(tmp_path, mode):
    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(
        tmp_path, _slice("one"),
    )
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one",
    )
    binding = _binding(promotion, snapshot, contract, approved, candidate)
    temp_root = tmp_path / "integration-temp"
    temp_root.mkdir(mode=0o700)
    temp_root.chmod(mode)

    with pytest.raises(
        promotion.IntegrationValidationError,
        match=r"(?i)temp root|private|permission|mode",
    ):
        _freeze_integration(
            promotion,
            tmp_path,
            snapshot,
            plan,
            contract,
            approved,
            (binding,),
            temp_root=temp_root,
        )

    assert tuple(temp_root.iterdir()) == ()
    assert _integration_refs(repo) == ()


def test_temp_root_rejects_non_host_owner_before_attempt_creation(
    tmp_path, monkeypatch,
):
    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(
        tmp_path, _slice("one"),
    )
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one",
    )
    binding = _binding(promotion, snapshot, contract, approved, candidate)
    temp_root = tmp_path / "integration-temp"
    temp_root.mkdir(mode=0o700)
    real_path_stat = Path.stat
    real_open = promotion.os.open
    real_os_stat = promotion.os.stat
    real_fstat = promotion.os.fstat
    temp_root_fds = set()

    class _WrongOwnerStat:
        def __init__(self, value):
            self._value = value
            self.st_uid = os.geteuid() + 1

        def __getattr__(self, name):
            return getattr(self._value, name)

    def wrong_path_owner(path, *args, **kwargs):
        result = real_path_stat(path, *args, **kwargs)
        if Path(path).absolute() == temp_root.absolute():
            return _WrongOwnerStat(result)
        return result

    def wrong_os_owner(path, *args, **kwargs):
        result = real_os_stat(path, *args, **kwargs)
        try:
            is_temp_root = (
                kwargs.get("dir_fd") is None
                and not isinstance(path, int)
                and Path(os.fsdecode(path)).absolute() == temp_root.absolute()
            )
        except (TypeError, ValueError):
            is_temp_root = False
        return _WrongOwnerStat(result) if is_temp_root else result

    def capture_temp_root_fd(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        try:
            resolved = Path(os.fsdecode(path)).resolve(strict=True)
        except (OSError, TypeError, ValueError):
            resolved = None
        if resolved == temp_root.resolve(strict=True):
            temp_root_fds.add(descriptor)
        return descriptor

    def wrong_fd_owner(descriptor):
        result = real_fstat(descriptor)
        if descriptor in temp_root_fds:
            return _WrongOwnerStat(result)
        return result

    monkeypatch.setattr(Path, "stat", wrong_path_owner)
    monkeypatch.setattr(promotion.os, "open", capture_temp_root_fd)
    monkeypatch.setattr(promotion.os, "stat", wrong_os_owner)
    monkeypatch.setattr(promotion.os, "fstat", wrong_fd_owner)

    with pytest.raises(
        promotion.IntegrationValidationError,
        match=r"(?i)temp root|owner",
    ):
        _freeze_integration(
            promotion,
            tmp_path,
            snapshot,
            plan,
            contract,
            approved,
            (binding,),
            temp_root=temp_root,
        )

    assert tuple(temp_root.iterdir()) == ()
    assert _integration_refs(repo) == ()


def test_temp_root_ancestor_swap_is_rejected_without_repository_mkdir(
    tmp_path, monkeypatch,
):
    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(
        tmp_path, _slice("one"),
    )
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one",
    )
    binding = _binding(promotion, snapshot, contract, approved, candidate)
    host_parent = tmp_path / "host-owned-parent"
    temp_root = host_parent / "one"
    temp_root.mkdir(parents=True, mode=0o700)
    displaced_parent = tmp_path / "displaced-host-owned-parent"
    forbidden_mkdir = []
    swapped = False
    real_mkdir = promotion.os.mkdir

    def swap_ancestor_before_attempt_mkdir(path, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        raw = os.fsdecode(path)
        if Path(raw).name.startswith("bestplan-integration-") and not swapped:
            host_parent.rename(displaced_parent)
            host_parent.symlink_to(repo, target_is_directory=True)
            swapped = True
            if dir_fd is None:
                forbidden_mkdir.append(Path(raw))
                raise AssertionError("attempt mkdir followed a swapped temp root")
        if dir_fd is None:
            return real_mkdir(path, mode)
        return real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(
        promotion.os,
        "mkdir",
        swap_ancestor_before_attempt_mkdir,
    )

    with pytest.raises(
        promotion.IntegrationValidationError,
        match=r"(?i)temp root|alias|changed|stable",
    ):
        _freeze_integration(
            promotion,
            tmp_path,
            snapshot,
            plan,
            contract,
            approved,
            (binding,),
            temp_root=temp_root,
        )

    assert swapped is True
    assert forbidden_mkdir == []
    assert not any(
        item.name.startswith("bestplan-integration-")
        for item in (repo / "one").iterdir()
    )
    assert _integration_refs(repo) == ()


def test_postverification_temp_root_swap_cannot_redirect_index_writes(
    tmp_path, monkeypatch,
):
    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(
        tmp_path, _slice("one"),
    )
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one",
    )
    binding = _binding(promotion, snapshot, contract, approved, candidate)
    host_parent = tmp_path / "host-owned-parent"
    temp_root = host_parent / "one"
    temp_root.mkdir(parents=True, mode=0o700)
    displaced_parent = tmp_path / "displaced-host-owned-parent"
    forbidden_attempt = None
    forbidden_index_writes = []
    real_create = promotion._create_owned_temp_attempt
    real_run_git = promotion._run_git

    def create_then_swap(root, *, deadline, cancel_event):
        nonlocal forbidden_attempt
        attempt = real_create(
            root,
            deadline=deadline,
            cancel_event=cancel_event,
        )
        forbidden_attempt = repo / "one" / attempt.leaf
        forbidden_attempt.mkdir(mode=0o700)
        host_parent.rename(displaced_parent)
        host_parent.symlink_to(repo, target_is_directory=True)
        return attempt

    def reject_redirected_index(repo_identity, *args, **kwargs):
        raw_index = (kwargs.get("extra_environment") or {}).get(
            "GIT_INDEX_FILE"
        )
        if raw_index is not None and forbidden_attempt is not None:
            index_path = Path(os.fsdecode(raw_index)).resolve(strict=False)
            if (
                index_path == forbidden_attempt
                or forbidden_attempt in index_path.parents
            ):
                forbidden_index_writes.append(index_path)
                raise promotion.PromotionError(
                    "integration index followed a swapped temp ancestor"
                )
        return real_run_git(repo_identity, *args, **kwargs)

    monkeypatch.setattr(
        promotion,
        "_create_owned_temp_attempt",
        create_then_swap,
    )
    monkeypatch.setattr(promotion, "_run_git", reject_redirected_index)

    try:
        _freeze_integration(
            promotion,
            tmp_path,
            snapshot,
            plan,
            contract,
            approved,
            (binding,),
            temp_root=temp_root,
        )
    except promotion.PromotionError:
        pass

    assert forbidden_attempt is not None
    assert forbidden_index_writes == []
    assert tuple(forbidden_attempt.iterdir()) == ()


def test_binding_slice_id_must_equal_manifest_slice_id(tmp_path):
    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(tmp_path, _slice("one"))
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one"
    )
    binding = _binding(promotion, snapshot, contract, approved, candidate)
    relabeled = _rebind(promotion, binding, slice_id="different-slice")

    with pytest.raises(promotion.IntegrationValidationError, match="slice"):
        _freeze_integration(
            promotion,
            tmp_path,
            snapshot,
            plan,
            contract,
            approved,
            (relabeled,),
        )

    assert _integration_refs(repo) == ()


def test_binding_ref_must_equal_host_candidate_ref_name(tmp_path):
    from agent.bestplan_candidates import candidate_ref_name

    promotion = _promotion()
    repo, snapshot, plan, contract, approved = _bundle(tmp_path, _slice("one"))
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one"
    )
    expected_ref = candidate_ref_name(
        "bp-integration", candidate.slice_id, candidate.attempt_id,
    )
    assert candidate.ref_name == expected_ref
    wrong_ref = candidate_ref_name(
        "bp-integration", candidate.slice_id, "different-attempt",
    )
    _git(repo, "update-ref", wrong_ref, candidate.commit_oid)
    binding = _binding(promotion, snapshot, contract, approved, candidate)
    relabeled = _rebind(promotion, binding, ref_name=wrong_ref)

    with pytest.raises(promotion.IntegrationValidationError, match="candidate.*ref|ref.*identity"):
        _freeze_integration(
            promotion,
            tmp_path,
            snapshot,
            plan,
            contract,
            approved,
            (relabeled,),
        )

    assert _integration_refs(repo) == ()


@pytest.mark.parametrize(
    "repository_field",
    (
        "repository_id",
        "workspace",
        "worktree",
        "git_dir",
        "common_dir",
        "common_dir_device",
        "common_dir_inode",
    ),
)
def test_contract_must_match_the_full_enrolled_repository_before_integration(
    tmp_path, repository_field,
):
    from agent.bestplan_contract import approval_digest

    promotion = _promotion()
    repo, snapshot, plan, contract, _approved = _bundle(
        tmp_path, _slice("one"),
    )
    altered = copy.deepcopy(contract)
    if repository_field == "repository_id":
        replacement_id = "different-repository"
        altered["repository"]["repository_id"] = replacement_id
        altered["publication"]["repository_id"] = replacement_id
        altered["live_target"]["repository_id"] = replacement_id
        altered["live_target"]["rollback"]["repository_id"] = replacement_id
        altered["controller"]["repository_id"] = replacement_id
    elif repository_field in {"workspace", "worktree", "git_dir", "common_dir"}:
        replacement = str(
            (tmp_path / f"different-{repository_field}").resolve()
        )
        altered["repository"][repository_field] = replacement
        altered["repository"][f"{repository_field}_raw_b64"] = (
            base64.b64encode(os.fsencode(replacement)).decode("ascii")
        )
    else:
        altered["repository"][repository_field] += 1
    approved = approval_digest(plan.to_manifest(), altered)
    candidate = _freeze_candidate(
        tmp_path, snapshot, plan_id="bp-integration", slice_id="one",
    )
    binding = _binding(promotion, snapshot, altered, approved, candidate)

    with pytest.raises(
        promotion.IntegrationValidationError,
        match=r"(?i)repository",
    ):
        _freeze_integration(
            promotion,
            tmp_path,
            snapshot,
            plan,
            altered,
            approved,
            (binding,),
        )

    assert _integration_refs(repo) == ()
