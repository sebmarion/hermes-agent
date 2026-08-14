"""Host adapter for exact-object BestPlan review evidence.

The adapter reads only immutable Git objects and durable receipts. It never
reads reviewed content from the ambient worktree.
"""

from __future__ import annotations

from dataclasses import dataclass
import difflib
import hashlib
import json
import math
import threading
import time
from typing import Any, Mapping, Sequence

from agent import bestplan_checks, bestplan_promotion
from agent.bestplan_checks import CheckSetReceipt
from agent.bestplan_contract import source_snapshot_digest
from agent.bestplan_promotion import FrozenIntegration
from agent.bestplan_source import SourceSnapshot
from agent.execution_plan import ExecutionPlan
from agent.review_engine import (
    EvidenceContext,
    REVIEW_PACKET_MAX_BYTES,
    ReviewArtifact,
    ReviewTarget,
    ReviewValidationError,
    build_review_packet,
)


_ACCEPTANCE_DOMAIN = b"hermes.bestplan.review-acceptance.v1\0"
_POLICY_DOMAIN = b"hermes.bestplan.review-policy.v1\0"
_MAX_OBJECT_BYTES = 16 * 1024 * 1024
_MAX_DIFF_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class BestplanReviewBundle:
    """One exact target, its immutable evidence, and canonical model packet."""

    target: ReviewTarget
    artifact: ReviewArtifact
    evidence: EvidenceContext
    packet: str
    diff_bytes: bytes


def bestplan_review_policy_digest(bindings: Sequence[object]) -> str:
    """Bind the fixed review rules to both exact resolved model lanes."""

    if not isinstance(bindings, (list, tuple)) or len(bindings) != 2:
        raise ReviewValidationError("BestPlan review requires exactly two lanes")
    lanes: list[dict[str, str]] = []
    for binding in bindings:
        values: dict[str, str] = {}
        for name in ("slot", "provider", "model", "model_family"):
            value = getattr(binding, name, None)
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ReviewValidationError("BestPlan review lane is invalid")
            values[name] = value.casefold() if name == "model_family" else value
        lanes.append(values)
    if [item["slot"] for item in lanes] != ["smart_reviewer", "code_worker"]:
        raise ReviewValidationError("BestPlan review lane order differs")
    if len({item["model_family"] for item in lanes}) != 2:
        raise ReviewValidationError("BestPlan review model families must differ")
    policy = {
        "blocking_severities": ["critical", "high"],
        "packet_schema": "hermes.bestplan.review-request.v1",
        "reviewer_lanes": lanes,
        "tools": [],
        "verdict_schema": "hermes.bestplan.review-verdict.v1",
    }
    return hashlib.sha256(_POLICY_DOMAIN + _canonical_bytes(policy)).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ReviewValidationError("BestPlan review evidence is not canonical JSON") from exc


def _require_deadline(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReviewValidationError("BestPlan review deadline is invalid")
    deadline = float(value)
    if not math.isfinite(deadline) or deadline <= time.monotonic():
        raise ReviewValidationError("BestPlan review deadline is invalid")
    return deadline


def _check_control(
    deadline: float,
    cancel_event: threading.Event | None,
) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ReviewValidationError("BestPlan review was cancelled")
    if time.monotonic() >= deadline:
        raise ReviewValidationError("BestPlan review deadline expired")


def _read_blob(
    snapshot: SourceSnapshot,
    oid: str,
    *,
    deadline: float,
    cancel_event: threading.Event | None,
) -> bytes:
    size_result = bestplan_promotion._run_git(
        snapshot.repo,
        "cat-file",
        "-s",
        oid,
        deadline=deadline,
        cancel_event=cancel_event,
    )
    try:
        size = int(size_result.stdout.strip())
    except ValueError as exc:
        raise bestplan_promotion.IntegrationProofStale(
            "review blob size is malformed"
        ) from exc
    if not 0 <= size <= _MAX_OBJECT_BYTES:
        raise ReviewValidationError("review blob exceeds the bounded size")
    data = bestplan_promotion._run_git(
        snapshot.repo,
        "cat-file",
        "blob",
        oid,
        deadline=deadline,
        cancel_event=cancel_event,
        output_limit=_MAX_OBJECT_BYTES,
    ).stdout
    if len(data) != size:
        raise bestplan_promotion.IntegrationProofStale(
            "review blob bytes are incomplete"
        )
    return data


def _path_text(path: bytes) -> str:
    try:
        return path.decode("utf-8", "strict")
    except UnicodeError as exc:
        raise ReviewValidationError("review path is not valid UTF-8") from exc


def _line_membership(
    before: bytes,
    after: bytes,
) -> tuple[frozenset[int], frozenset[int]]:
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    changed_after: set[int] = set()
    deleted_before: set[int] = set()
    matcher = difflib.SequenceMatcher(
        None,
        before_lines,
        after_lines,
        autojunk=False,
    )
    for tag, first_start, first_end, second_start, second_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        changed_after.update(range(second_start + 1, second_end + 1))
        deleted_before.update(range(first_start + 1, first_end + 1))
    return frozenset(changed_after), frozenset(deleted_before)


def _normalized_plan_paths(
    plan: ExecutionPlan,
    snapshot: SourceSnapshot,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    leases: set[str] = set()
    expected: set[str] = set()
    for item in plan.slices:
        prefix = bestplan_promotion._slice_prefix(item, snapshot)
        for value in item.allowed_paths:
            raw = bestplan_promotion._manifest_path(
                prefix, value, "review lease"
            )
            text = _path_text(raw)
            leases.add(text + "/" if value.endswith("/") else text)
        for value in item.expected_artifacts:
            expected.add(
                _path_text(
                    bestplan_promotion._manifest_path(
                        prefix, value, "review expected artifact"
                    )
                )
            )
    return tuple(sorted(leases)), tuple(sorted(expected))


def _check_receipt_bytes(checks: CheckSetReceipt) -> bytes:
    return _canonical_bytes(
        {
            "contract_digest": checks.contract_digest,
            "integration_oid": checks.integration_oid,
            "ordered_receipts": [
                {
                    "command_digest": item.command_digest,
                    "command_id": item.command_id,
                    "exit_code": item.exit_code,
                    "output_framed_sha256": item.output_framed_sha256,
                    "policy_digest": item.policy_digest,
                    "post_tree_digest": item.post_tree_digest,
                    "pre_tree_digest": item.pre_tree_digest,
                    "receipt_digest": item.receipt_digest,
                    "stderr_sha256": item.stderr_sha256,
                    "stderr_size": item.stderr_size,
                    "stdout_sha256": item.stdout_sha256,
                    "stdout_size": item.stdout_size,
                }
                for item in checks.ordered_receipts
            ],
            "receipt_digest": checks.receipt_digest,
            "schema": "hermes.bestplan.check-set.v1",
        }
    )


def _validate_receipts(
    *,
    plan_id: str,
    snapshot: SourceSnapshot,
    integration: FrozenIntegration,
    checks: CheckSetReceipt,
    approval_digest: str,
    contract_digest: str,
    deadline: float,
    cancel_event: threading.Event | None,
) -> None:
    if not isinstance(integration, FrozenIntegration):
        raise ReviewValidationError("review integration is invalid")
    if not isinstance(checks, CheckSetReceipt):
        raise ReviewValidationError("review check receipt is invalid")
    if (
        integration.plan_id != plan_id
        or integration.approval_digest != approval_digest
        or integration.contract_digest != contract_digest
        or integration.source_snapshot_digest != source_snapshot_digest(snapshot)
        or bestplan_promotion._receipt_digest(integration)
        != integration.receipt_digest
    ):
        raise ReviewValidationError("review integration receipt differs")
    expected_check_digest = bestplan_checks._domain_digest(
        b"hermes.bestplan.check-set.v1",
        {
            "integration_oid": checks.integration_oid,
            "contract_digest": checks.contract_digest,
            "ordered_receipts": [item.receipt_digest for item in checks.ordered_receipts],
        },
    )
    if (
        checks.integration_oid != integration.integration_oid
        or checks.contract_digest != contract_digest
        or checks.receipt_digest != expected_check_digest
    ):
        raise ReviewValidationError("review check receipt differs")
    if bestplan_promotion._read_ref(
        snapshot.repo,
        integration.ref_name,
        deadline=deadline,
        cancel_event=cancel_event,
    ) != integration.integration_oid:
        raise bestplan_promotion.IntegrationProofStale(
            "review integration ref changed"
        )
    if bestplan_promotion._read_ref(
        snapshot.repo,
        integration.target_ref,
        deadline=deadline,
        cancel_event=cancel_event,
    ) != integration.target_oid:
        raise bestplan_promotion.IntegrationProofStale("review target ref changed")
    tree_oid, parents = bestplan_promotion._commit_proof(
        snapshot.repo,
        integration.integration_oid,
        deadline=deadline,
        cancel_event=cancel_event,
    )
    if tree_oid != integration.tree_oid or parents != (integration.target_oid,):
        raise bestplan_promotion.IntegrationProofStale(
            "review integration commit differs"
        )


def build_bestplan_review_bundle(
    *,
    plan_id: str,
    generation: int,
    raw_request: str,
    plan: ExecutionPlan,
    snapshot: SourceSnapshot,
    integration: FrozenIntegration,
    checks: CheckSetReceipt,
    contract: Mapping[str, Any],
    approval_digest: str,
    policy_digest: str,
    dispositions: Sequence[Mapping[str, object]] = (),
    deadline: float,
    cancel_event: threading.Event | None = None,
) -> BestplanReviewBundle:
    """Build one bounded packet from frozen Git objects and exact receipts."""

    deadline = _require_deadline(deadline)
    if cancel_event is not None and not isinstance(cancel_event, threading.Event):
        raise ReviewValidationError("BestPlan review cancellation input is invalid")
    _check_control(deadline, cancel_event)
    if not isinstance(plan, ExecutionPlan) or not isinstance(snapshot, SourceSnapshot):
        raise ReviewValidationError("BestPlan review plan or source is invalid")
    if not isinstance(raw_request, str) or not raw_request.strip():
        raise ReviewValidationError("BestPlan review request is empty")
    if not isinstance(contract, Mapping):
        raise ReviewValidationError("BestPlan review contract is invalid")
    contract_digest = getattr(integration, "contract_digest", None)
    if not isinstance(contract_digest, str):
        raise ReviewValidationError("review integration receipt differs")
    try:
        exact_contract_digest = (
            bestplan_promotion._validate_task6_contract(contract).contract_digest
        )
    except Exception as exc:
        raise ReviewValidationError("BestPlan review contract is invalid") from exc
    if exact_contract_digest != contract_digest:
        raise ReviewValidationError("BestPlan review contract differs")
    _validate_receipts(
        plan_id=plan_id,
        snapshot=snapshot,
        integration=integration,
        checks=checks,
        approval_digest=approval_digest,
        contract_digest=contract_digest,
        deadline=deadline,
        cancel_event=cancel_event,
    )

    diff = bestplan_promotion._run_git(
        snapshot.repo,
        "diff",
        "--binary",
        "--full-index",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
        integration.target_oid,
        integration.integration_oid,
        "--",
        deadline=deadline,
        cancel_event=cancel_event,
        output_limit=_MAX_DIFF_BYTES,
    ).stdout
    diff_sha256 = hashlib.sha256(diff).hexdigest()
    acceptance_payload = {
        "manifest": plan.to_manifest(),
        "request": raw_request,
    }
    acceptance_digest = hashlib.sha256(
        _ACCEPTANCE_DOMAIN + _canonical_bytes(acceptance_payload)
    ).hexdigest()
    target = ReviewTarget.bestplan_integration(
        plan_id=plan_id,
        generation=generation,
        base_oid=snapshot.head_oid,
        local_target_oid=integration.target_oid,
        integration_oid=integration.integration_oid,
        integration_tree_oid=integration.tree_oid,
        integration_ref=integration.ref_name,
        integration_receipt_digest=integration.receipt_digest,
        check_receipt_digest=checks.receipt_digest,
        approval_digest=approval_digest,
        contract_digest=contract_digest,
        diff_sha256=diff_sha256,
        acceptance_digest=acceptance_digest,
        policy_digest=policy_digest,
    )

    target_tree_oid, _parents = bestplan_promotion._commit_proof(
        snapshot.repo,
        integration.target_oid,
        deadline=deadline,
        cancel_event=cancel_event,
    )
    before_tree = bestplan_promotion._tree_map(
        snapshot.repo,
        target_tree_oid,
        deadline=deadline,
        cancel_event=cancel_event,
    )
    after_tree = bestplan_promotion._tree_map(
        snapshot.repo,
        integration.tree_oid,
        deadline=deadline,
        cancel_event=cancel_event,
    )
    raw_changed_paths = bestplan_promotion._changed_paths(
        before_tree,
        after_tree,
        deadline=deadline,
        cancel_event=cancel_event,
    )
    changed_paths = tuple(_path_text(item) for item in raw_changed_paths)
    after_blobs: dict[str, bytes] = {}
    before_blobs: dict[str, bytes] = {}
    changed_after_lines: dict[str, frozenset[int]] = {}
    deleted_before_lines: dict[str, frozenset[int]] = {}
    for raw_path in raw_changed_paths:
        _check_control(deadline, cancel_event)
        path = _path_text(raw_path)
        before_entry = before_tree.get(raw_path)
        after_entry = after_tree.get(raw_path)
        before = (
            b""
            if before_entry is None
            else _read_blob(
                snapshot,
                before_entry.oid,
                deadline=deadline,
                cancel_event=cancel_event,
            )
        )
        after = (
            b""
            if after_entry is None
            else _read_blob(
                snapshot,
                after_entry.oid,
                deadline=deadline,
                cancel_event=cancel_event,
            )
        )
        if before_entry is not None:
            before_blobs[path] = before
        if after_entry is not None:
            after_blobs[path] = after
        after_lines, before_lines = _line_membership(before, after)
        changed_after_lines[path] = after_lines
        deleted_before_lines[path] = before_lines

    leases, expected_artifacts = _normalized_plan_paths(plan, snapshot)
    after_entries = {_path_text(path): entry for path, entry in after_tree.items()}
    after_paths = set(after_entries)
    missing_artifacts = tuple(
        path for path in expected_artifacts if path not in after_paths
    )
    deleted_paths = tuple(
        path for path in changed_paths if path not in after_blobs
    )
    unchanged_dependencies: list[str] = []
    unchanged_dependency_text: dict[str, str] = {}
    for path in expected_artifacts:
        if path in changed_paths or path not in after_entries:
            continue
        data = _read_blob(
            snapshot,
            after_entries[path].oid,
            deadline=deadline,
            cancel_event=cancel_event,
        )
        try:
            text = data.decode("utf-8", "strict")
        except UnicodeError:
            continue
        if not text or "\x00" in text:
            continue
        after_blobs[path] = data
        unchanged_dependencies.append(path)
        unchanged_dependency_text[path] = text
    contract_receipts = {
        "execution_contract": _canonical_bytes(contract),
        "frozen_integration": _canonical_bytes(
            {
                "integration_oid": integration.integration_oid,
                "receipt_digest": integration.receipt_digest,
                "ref_name": integration.ref_name,
                "target_oid": integration.target_oid,
                "tree_oid": integration.tree_oid,
            }
        ),
        "check_set": _check_receipt_bytes(checks),
    }

    def read_frozen_file(path: str) -> bytes:
        try:
            return after_blobs[path]
        except KeyError as exc:
            raise ReviewValidationError("frozen review file is unavailable") from exc

    def diff_membership(path: str, start_line: int, end_line: int) -> bool:
        changed = changed_after_lines.get(path, frozenset())
        return bool(changed) and all(
            line in changed for line in range(start_line, end_line + 1)
        )

    evidence_kwargs: dict[str, object] = {
        "read_frozen_file": read_frozen_file,
        "diff_membership": diff_membership,
        "approved_lease_paths": leases,
        "missing_artifacts": missing_artifacts,
        "deleted_paths": deleted_paths,
        "unchanged_dependencies": tuple(unchanged_dependencies),
        "contract_receipts": contract_receipts,
    }
    # The review engine's tagged deletion-line API is versioned separately.
    # Supply it when present while retaining compatibility with an older local
    # engine during rolling upgrades.
    fields = getattr(EvidenceContext, "__dataclass_fields__", {})
    if "read_frozen_base_file" in fields:
        def read_frozen_base_file(path: str) -> bytes:
            try:
                return before_blobs[path]
            except KeyError as exc:
                raise ReviewValidationError(
                    "frozen review base file is unavailable"
                ) from exc

        def deleted_line_membership(
            path: str, start_line: int, end_line: int
        ) -> bool:
            deleted = deleted_before_lines.get(path, frozenset())
            return bool(deleted) and all(
                line in deleted for line in range(start_line, end_line + 1)
            )

        evidence_kwargs["read_frozen_base_file"] = read_frozen_base_file
        evidence_kwargs["deleted_line_membership"] = deleted_line_membership
    evidence = EvidenceContext(**evidence_kwargs)
    issue_catalog: dict[str, dict[str, str]] = {}
    for kind, identifiers in (
        ("missing_artifact", missing_artifacts),
        ("deleted_path", deleted_paths),
        ("contract_or_receipt", tuple(contract_receipts)),
    ):
        for identifier in identifiers:
            locator_id = evidence.issue_locator(kind, identifier)
            issue_catalog[locator_id] = {
                "kind": kind,
                "identifier": identifier,
            }
            if kind == "contract_or_receipt":
                try:
                    quoted_evidence = contract_receipts[identifier].decode(
                        "utf-8", "strict"
                    )
                except UnicodeError as exc:
                    raise ReviewValidationError(
                        "contract receipt evidence is not UTF-8"
                    ) from exc
                issue_catalog[locator_id]["quoted_evidence"] = quoted_evidence
    for path in unchanged_dependencies:
        locator_id = hashlib.sha256(
            b"hermes.bestplan.review-unchanged-dependency.v1\0"
            + path.encode("utf-8")
        ).hexdigest()
        issue_catalog[locator_id] = {
            "kind": "unchanged_dependency",
            "identifier": path,
            "quoted_evidence": unchanged_dependency_text[path],
        }
    acceptance = tuple(
        f"{item.id}: {criterion}"
        for item in plan.slices
        for criterion in item.acceptance
    )
    rules = (
        f"Merge policy: {plan.merge_policy}",
        f"Stop condition: {plan.stop_condition}",
        "CRITICAL and HIGH findings block this exact generation.",
        "Return only the required strict JSON verdict and do not use tools.",
    )
    artifact = ReviewArtifact.build(
        target=target,
        diff_bytes=diff,
        task=raw_request,
        acceptance=acceptance,
        rules=rules,
        issue_locator_catalog=issue_catalog,
        dispositions=dispositions,
    )
    packet = build_review_packet(target, artifact=artifact)
    if len(packet.encode("utf-8")) > REVIEW_PACKET_MAX_BYTES:
        raise ReviewValidationError("final review packet is too large")
    return BestplanReviewBundle(
        target=target,
        artifact=artifact,
        evidence=evidence,
        packet=packet,
        diff_bytes=diff,
    )
