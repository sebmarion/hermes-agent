from __future__ import annotations

import base64
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest


def _git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        input=input_bytes,
        check=True,
        capture_output=True,
    ).stdout


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "review@example.test")
    _git(path, "config", "user.name", "Review Test")
    (path / "src").mkdir()
    (path / "src/app.py").write_text("alpha\nbeta\n", encoding="utf-8")
    (path / "src/dependency.py").write_text(
        "def required_guard():\n    return True\n",
        encoding="utf-8",
    )
    (path / "gone.txt").write_text("old one\nold two\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "base")
    return path


def _snapshot(repo: Path):
    from agent import bestplan_source

    identity = bestplan_source.resolve_repo_identity(str(repo))
    return bestplan_source.capture_source_snapshot(
        identity,
        time.monotonic() + bestplan_source.DEFAULT_SOURCE_OPERATION_SECONDS,
    )


def _plan(repo: Path):
    from agent.execution_plan import compile_execution_plan

    return compile_execution_plan(
        {
            "version": 1,
            "mode": "delegate",
            "risk": "high",
            "slices": [
                {
                    "id": "implementation",
                    "kind": "implement",
                    "goal": "Fix the exact implementation defect",
                    "depends_on": [],
                    "capability": "fast_fallback",
                    "workspace": str(repo),
                    "allowed_paths": ["src/", "gone.txt", "asset.bin"],
                    "read_only": False,
                    "expected_artifacts": ["src/app.py", "src/dependency.py"],
                    "acceptance": ["pytest -q -- tests/test_app.py::test_fix"],
                }
            ],
            "merge_policy": "apply independent candidates in manifest order",
            "stop_condition": "all acceptance conditions pass",
            "escalation_predicates": ["review_blocker"],
        }
    )


def _frozen_fixture(tmp_path: Path):
    from agent import bestplan_checks, bestplan_promotion
    from agent.bestplan_contract import (
        BoundCommand,
        ControllerIdentity,
        canonical_json,
        source_snapshot_digest,
    )
    from agent.bestplan_local import build_local_go_contract, local_go_contract_digest

    repo = _repo(tmp_path / "repo")
    snapshot = _snapshot(repo)
    plan = _plan(repo)
    (repo / "src/app.py").write_text("alpha\nfixed\nnew\n", encoding="utf-8")
    (repo / "gone.txt").unlink()
    (repo / "asset.bin").write_bytes(b"\x00\xff\x10binary\n")
    _git(repo, "add", "-A")
    tree_oid = _git(repo, "write-tree").strip().decode("ascii")
    integration_oid = _git(
        repo,
        "commit-tree",
        tree_oid,
        "-p",
        snapshot.head_oid,
        input_bytes=b"review integration\n",
    ).strip().decode("ascii")
    ref_name = "refs/hermes-bestplan-integrations/review-test/integration"
    _git(repo, "update-ref", ref_name, integration_oid)
    _git(repo, "reset", "--hard", "-q", snapshot.head_oid)

    approval_digest = "a" * 64
    executable = Path(sys.executable).resolve()
    contract = build_local_go_contract(
        snapshot=snapshot,
        controller=ControllerIdentity(
            repository_id=snapshot.repo.repository_id,
            controller_id="review-controller",
            release_oid=snapshot.head_oid,
            artifact_sha256="e" * 64,
        ),
        commands=(
            BoundCommand(
                identifier="focused-tests",
                executable=str(executable),
                executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
                argv=("-I", "-c", "raise SystemExit(0)"),
                logical_cwd="integration",
                env=(("PYTHONHASHSEED", "0"),),
                inputs=(),
                cache=(),
                timeout_seconds=30,
                network_allowlist=(),
            ),
        ),
        manifest_digest=hashlib.sha256(
            canonical_json(plan.to_manifest()).encode("utf-8")
        ).hexdigest(),
        check_runtime_digest="f" * 64,
    )
    contract_digest = local_go_contract_digest(contract)
    integration = bestplan_promotion.FrozenIntegration(
        plan_id="review-test",
        approval_digest=approval_digest,
        contract_digest=contract_digest,
        source_snapshot_digest=source_snapshot_digest(snapshot),
        target_ref="refs/heads/main",
        target_oid=snapshot.head_oid,
        integration_oid=integration_oid,
        tree_oid=tree_oid,
        ref_name=ref_name,
        candidates=(),
        receipt_digest="",
    )
    integration = replace(
        integration,
        receipt_digest=bestplan_promotion._receipt_digest(integration),
    )
    set_body = {
        "integration_oid": integration_oid,
        "contract_digest": contract_digest,
        "ordered_receipts": [],
    }
    checks = bestplan_checks.CheckSetReceipt(
        integration_oid=integration_oid,
        contract_digest=contract_digest,
        ordered_receipts=(),
        receipt_digest=bestplan_checks._domain_digest(
            b"hermes.bestplan.check-set.v1", set_body
        ),
    )
    return {
        "repo": repo,
        "snapshot": snapshot,
        "plan": plan,
        "integration": integration,
        "checks": checks,
        "approval_digest": approval_digest,
        "contract_digest": contract_digest,
        "contract": contract,
    }


def _build(fixture: dict, **overrides):
    from agent.bestplan_review import build_bestplan_review_bundle

    values = {
        "plan_id": "review-test",
        "generation": 0,
        "raw_request": "Fix the application defect",
        "plan": fixture["plan"],
        "snapshot": fixture["snapshot"],
        "integration": fixture["integration"],
        "checks": fixture["checks"],
        "contract": fixture["contract"],
        "approval_digest": fixture["approval_digest"],
        "policy_digest": "c" * 64,
        "dispositions": (),
        "deadline": time.monotonic() + 20,
        "cancel_event": None,
    }
    values.update(overrides)
    return build_bestplan_review_bundle(**values)


def test_bundle_binds_exact_git_objects_and_ignores_ambient_worktree(tmp_path):
    fixture = _frozen_fixture(tmp_path)
    repo = fixture["repo"]
    (repo / "src/app.py").write_text("ambient unreviewed bytes\n", encoding="utf-8")
    (repo / "untracked-secret.txt").write_text("not in target\n", encoding="utf-8")

    bundle = _build(fixture)

    expected_diff = _git(
        repo,
        "diff",
        "--binary",
        "--full-index",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
        fixture["integration"].target_oid,
        fixture["integration"].integration_oid,
        "--",
    )
    packet = json.loads(bundle.packet)
    encoded = packet["artifact"]["git_diff"]["content_base64"]
    assert base64.b64decode(encoded) == expected_diff
    assert bundle.diff_bytes == expected_diff
    assert bundle.target.diff_sha256 == hashlib.sha256(expected_diff).hexdigest()
    assert bundle.evidence.frozen_file_bytes("src/app.py") == b"alpha\nfixed\nnew\n"
    assert b"ambient unreviewed bytes" not in bundle.packet.encode("utf-8")
    assert b"not in target" not in bundle.packet.encode("utf-8")


def test_bundle_evidence_validates_a_changed_line_against_frozen_bytes(tmp_path):
    from agent.review_engine import parse_review_verdict

    fixture = _frozen_fixture(tmp_path)
    bundle = _build(fixture)
    raw = json.dumps(
        {
            "schema": "hermes.bestplan.review-verdict.v1",
            "target_digest": bundle.target.target_digest,
            "integration_oid": bundle.target.integration_oid,
            "findings": [
                {
                    "severity": "medium",
                    "locator": {
                        "kind": "changed_lines",
                        "path": "src/app.py",
                        "start_line": 2,
                        "end_line": 2,
                        "quoted_evidence": "fixed\n",
                    },
                    "title": "Concrete changed-line observation",
                    "trigger": "The reviewed function executes",
                    "observed_failure": "The changed value needs follow-up",
                    "blast_radius": "The application module",
                    "reproduction": {
                        "kind": "not_applicable",
                        "reason": "Advisory review observation",
                    },
                }
            ],
        }
    )

    verdict = parse_review_verdict(
        raw,
        target=bundle.target,
        evidence=bundle.evidence,
    )

    assert verdict.passed is True
    assert verdict.findings[0].cited_bytes_sha256 == hashlib.sha256(b"fixed\n").hexdigest()


def test_bundle_evidence_validates_deleted_lines_from_the_frozen_base(tmp_path):
    from agent.review_engine import parse_review_verdict

    fixture = _frozen_fixture(tmp_path)
    bundle = _build(fixture)
    raw = json.dumps(
        {
            "schema": "hermes.bestplan.review-verdict.v1",
            "target_digest": bundle.target.target_digest,
            "integration_oid": bundle.target.integration_oid,
            "findings": [
                {
                    "severity": "high",
                    "locator": {
                        "kind": "deleted_lines",
                        "path": "src/app.py",
                        "before_start_line": 2,
                        "before_end_line": 2,
                        "quoted_evidence": "beta\n",
                    },
                    "title": "Required behavior was deleted",
                    "trigger": "The changed application path runs",
                    "observed_failure": "The prior behavior is no longer present",
                    "blast_radius": "All callers of the application module",
                    "reproduction": {
                        "kind": "not_applicable",
                        "reason": "The exact deletion is sufficient evidence",
                    },
                }
            ],
        }
    )

    verdict = parse_review_verdict(
        raw,
        target=bundle.target,
        evidence=bundle.evidence,
    )

    assert verdict.passed is False
    assert verdict.blocking_findings[0].locator.kind == "deleted_lines"
    assert verdict.blocking_findings[0].cited_bytes_sha256 == hashlib.sha256(
        b"beta\n"
    ).hexdigest()


def test_bundle_packet_makes_exact_contract_receipt_evidence_usable(tmp_path):
    from agent.review_engine import parse_review_verdict

    fixture = _frozen_fixture(tmp_path)
    bundle = _build(fixture)
    packet = json.loads(bundle.packet)
    locator_id, locator = next(
        (locator_id, item)
        for locator_id, item in packet["artifact"]["issue_locator_catalog"].items()
        if item["kind"] == "contract_or_receipt"
        and item["identifier"] == "check_set"
    )

    assert locator["quoted_evidence"].encode("utf-8") == (
        bundle.evidence.contract_receipts["check_set"]
    )
    raw = json.dumps(
        {
            "schema": "hermes.bestplan.review-verdict.v1",
            "target_digest": bundle.target.target_digest,
            "integration_oid": bundle.target.integration_oid,
            "findings": [
                {
                    "severity": "high",
                    "locator": {
                        "kind": "contract_or_receipt",
                        "locator_id": locator_id,
                        "quoted_evidence": locator["quoted_evidence"],
                    },
                    "title": "The exact check receipt proves a blocker",
                    "trigger": "The checked integration is promoted",
                    "observed_failure": "The receipt omits required evidence",
                    "blast_radius": "The exact BestPlan generation",
                    "reproduction": {
                        "kind": "not_applicable",
                        "reason": "The immutable receipt is the evidence",
                    },
                }
            ],
        }
    )

    verdict = parse_review_verdict(
        raw,
        target=bundle.target,
        evidence=bundle.evidence,
    )

    assert verdict.passed is False
    assert verdict.blocking_findings[0].locator.path == "check_set"


def test_bundle_packet_makes_frozen_unchanged_dependency_evidence_usable(tmp_path):
    from agent.review_engine import parse_review_verdict

    fixture = _frozen_fixture(tmp_path)
    bundle = _build(fixture)
    packet = json.loads(bundle.packet)
    locator = next(
        item
        for item in packet["artifact"]["issue_locator_catalog"].values()
        if item["kind"] == "unchanged_dependency"
        and item["identifier"] == "src/dependency.py"
    )

    assert locator["quoted_evidence"] == (
        "def required_guard():\n    return True\n"
    )
    assert bundle.evidence.unchanged_dependencies == ("src/dependency.py",)
    raw = json.dumps(
        {
            "schema": "hermes.bestplan.review-verdict.v1",
            "target_digest": bundle.target.target_digest,
            "integration_oid": bundle.target.integration_oid,
            "findings": [
                {
                    "severity": "high",
                    "locator": {
                        "kind": "unchanged_dependency",
                        "path": "src/dependency.py",
                        "start_line": 1,
                        "end_line": 2,
                        "quoted_evidence": locator["quoted_evidence"],
                    },
                    "title": "An unchanged dependency rejects the new behavior",
                    "trigger": "The changed application calls this guard",
                    "observed_failure": "The dependency still enforces the old contract",
                    "blast_radius": "All callers of the changed application",
                    "reproduction": {
                        "kind": "not_applicable",
                        "reason": "The immutable dependency bytes are sufficient",
                    },
                }
            ],
        }
    )

    verdict = parse_review_verdict(
        raw,
        target=bundle.target,
        evidence=bundle.evidence,
    )

    assert verdict.passed is False
    assert verdict.blocking_findings[0].path == "src/dependency.py"


def test_bundle_rejects_a_moved_owned_integration_ref(tmp_path):
    from agent.bestplan_promotion import IntegrationProofStale

    fixture = _frozen_fixture(tmp_path)
    _git(
        fixture["repo"],
        "update-ref",
        fixture["integration"].ref_name,
        fixture["integration"].target_oid,
    )

    with pytest.raises(IntegrationProofStale, match="review integration ref changed"):
        _build(fixture)


def test_bundle_rejects_a_check_receipt_for_another_integration(tmp_path):
    fixture = _frozen_fixture(tmp_path)
    stale_checks = replace(fixture["checks"], integration_oid="d" * 40)

    with pytest.raises(ValueError, match="review check receipt differs"):
        _build(fixture, checks=stale_checks)


def test_acceptance_digest_changes_with_the_original_request(tmp_path):
    fixture = _frozen_fixture(tmp_path)

    first = _build(fixture, raw_request="Fix defect A")
    second = _build(fixture, raw_request="Fix defect B")

    assert first.target.acceptance_digest != second.target.acceptance_digest
    assert first.packet != second.packet


def test_review_policy_digest_binds_both_exact_distinct_lanes():
    from agent.bestplan_review import bestplan_review_policy_digest

    bindings = (
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

    baseline = bestplan_review_policy_digest(bindings)

    assert len(baseline) == 64
    changed = (
        bindings[0],
        SimpleNamespace(**{**bindings[1].__dict__, "model": "gpt-review-new"}),
    )
    assert bestplan_review_policy_digest(changed) != baseline
