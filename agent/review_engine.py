"""Exact-target review primitives for BestPlan and manual ``/review``.

This module contains the policy-neutral foundation shared by both entry
adapters.  It deliberately has no Agent, WebUI, Git, or model-provider side
effects.  Callers supply frozen target identities and a reviewer-call
function; the host validates all returned evidence and derives the verdict.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import difflib
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import shlex
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
import secrets
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, Sequence


_TARGET_DOMAIN = b"hermes.bestplan.review-target.v1\0"
_OUTPUT_DOMAIN = b"hermes.bestplan.review-output.v1\0"
_RECEIPT_DOMAIN = b"hermes.bestplan.review-receipt.v1\0"
_EVENT_PAYLOAD_DOMAIN = b"hermes.bestplan.review-event-payload.v1\0"
_EVENT_DOMAIN = b"hermes.bestplan.review-event.v1\0"
_FINDING_DOMAIN = b"hermes.bestplan.review-finding.v1\0"
_ISSUE_LOCATOR_DOMAIN = b"hermes.bestplan.review-issue-locator.v1\0"
_STORE_EVENT_DOMAIN = b"hermes.bestplan.review-store-event.v1\0"
_PASS_SLOTS_DOMAIN = b"hermes.bestplan.review-pass-slots.v1\0"
_REPAIR_CANDIDATE_DOMAIN = b"hermes.bestplan.repair-candidate.v1\0"
_BESTPLAN_CHANGED_PATHS_DOMAIN = b"hermes.bestplan.changed-paths.v1\0"
_REQUIRED_SLOTS = ("smart_reviewer", "code_worker")
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_HEX_40 = re.compile(r"^[0-9a-f]{40}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_SUPPORTED_LOCATOR_KINDS = {
    "changed_lines",
    "deleted_lines",
    "missing_artifact",
    "deleted_path",
    "unchanged_dependency",
    "contract_or_receipt",
}
_MAX_FROZEN_FILE_BYTES = 16 * 1024 * 1024
_MAX_FINDINGS = 128
REVIEW_PACKET_MAX_BYTES = 256 * 1024
_MAX_REVIEW_ARTIFACT_FIELD_BYTES = 2 * REVIEW_PACKET_MAX_BYTES
_MAX_REVIEW_DIFF_BYTES = 8 * 1024 * 1024
_MAX_REVIEW_LIST_ITEMS = 256
_MAX_RECOVERY_METADATA_BYTES = 1024 * 1024
_MANUAL_ADAPTER_VERSION = "manual_snapshot.v1"
_MANUAL_SNAPSHOT_SCHEMA = "hermes.manual-review-snapshot.v1"
_MANUAL_SNAPSHOT_MAX_FILES = 256
_MANUAL_SNAPSHOT_MAX_BYTES = 8 * 1024 * 1024
_MANUAL_SNAPSHOT_RETAIN = 64
_MANUAL_ZERO_DIGEST = "0" * 64
_MANUAL_SNAPSHOT_DOMAIN = b"hermes.manual-review-snapshot.v1\0"
_MANUAL_ACCEPTANCE_DOMAIN = b"hermes.manual-review-acceptance.v1\0"
_MANUAL_REPOSITORY_DOMAIN = b"hermes.manual-review-repository.v1\0"
_MANUAL_JOB_DOMAIN = b"hermes.manual-review-job.v1\0"
_MANUAL_VERDICT_DOMAIN = b"hermes.manual-review-stored-verdict.v1\0"
_MANUAL_REVIEW_LEASE_NS = 600_000_000_000
_MANUAL_REVIEW_HEARTBEAT_SECONDS = 10.0
_MANUAL_REVIEW_CONTROL_POLL_SECONDS = 0.05
_MANUAL_CANCEL_EXTINCTION_SECONDS = 2.0
_MANUAL_CANCEL_REQUEST_RETRY_BASE_SECONDS = 0.05
_MANUAL_CANCEL_REQUEST_RETRY_MAX_SECONDS = 5.0
_MANUAL_CANCEL_FINALIZE_RETRY_BASE_SECONDS = 0.05
_MANUAL_CANCEL_FINALIZE_RETRY_MAX_SECONDS = 5.0
_REVIEW_ENQUEUE_RETRY_BASE_SECONDS = 0.25
_REVIEW_ENQUEUE_RETRY_MAX_SECONDS = 30.0
_REVIEW_ENQUEUE_RETRY_LOCK = threading.Lock()
_REVIEW_ENQUEUE_RETRY_ATTEMPTS: dict[tuple[str, str, str], int] = {}
_REVIEW_ENQUEUE_RETRY_SCHEDULED: set[tuple[str, str, str]] = set()
_LANDING_AUTHORITY_KEY = os.urandom(32)


class ReviewValidationError(ValueError):
    """The review input or reviewer output violates the host contract."""


class ReviewJournalConflict(RuntimeError):
    """An operation ID was reused with different immutable event data."""


class ReviewRequiresAuthority(ReviewValidationError):
    """Valid review evidence names a changed path outside the approved lease."""


class ReviewStoreConflict(RuntimeError):
    """Durable review state conflicts with an immutable prior operation."""


class ReviewLeaseConflict(ReviewStoreConflict):
    """A durable review mutation does not own the current fencing token."""


class _ManualInvocationCancelled(RuntimeError):
    """The current manual invocation durably cancelled its exact review job."""


@dataclass(frozen=True)
class LandingAuthorization:
    """One process-bound, one-shot authority for the local Git effect."""

    plan_id: str
    review_job_id: str
    target_digest: str
    integration_oid: str
    check_receipt_digest: str
    fencing_token: int
    owner_pid: int
    owner_process_start_id: str
    repository_id: str
    repository_effect_lock_path: str
    authorization_digest: str
    _lock_handle: object = field(repr=False, compare=False)

    def validate_digest(self) -> bool:
        return hmac.compare_digest(
            self.authorization_digest,
            _landing_authorization_digest(_landing_authorization_payload(self)),
        )

    def release_effect_lock(self) -> None:
        """Release this operation's repository effect lock."""

        close = getattr(self._lock_handle, "close", None)
        if callable(close):
            close()


def _landing_authorization_payload(value: object) -> dict[str, object]:
    return {
        "check_receipt_digest": getattr(value, "check_receipt_digest"),
        "fencing_token": getattr(value, "fencing_token"),
        "integration_oid": getattr(value, "integration_oid"),
        "owner_pid": getattr(value, "owner_pid"),
        "owner_process_start_id": getattr(value, "owner_process_start_id"),
        "plan_id": getattr(value, "plan_id"),
        "repository_effect_lock_path": getattr(
            value, "repository_effect_lock_path"
        ),
        "repository_id": getattr(value, "repository_id"),
        "review_job_id": getattr(value, "review_job_id"),
        "target_digest": getattr(value, "target_digest"),
    }


def _landing_authorization_digest(payload: Mapping[str, object]) -> str:
    return hmac.new(
        _LANDING_AUTHORITY_KEY,
        _canonical_json(dict(payload)).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _issue_landing_authorization(
    *, lock_handle: object, **values: object
) -> LandingAuthorization:
    payload = dict(values)
    return LandingAuthorization(
        **payload,
        authorization_digest=_landing_authorization_digest(payload),
        _lock_handle=lock_handle,
    )


class ReviewNoTarget(ReviewValidationError):
    """No immutable target can be reconstructed for a manual review."""


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ReviewValidationError("review data must be canonical JSON") from exc


def _domain_digest(domain: bytes, canonical_json: str) -> str:
    return hashlib.sha256(domain + canonical_json.encode("utf-8")).hexdigest()


def _require_text(value: object, field_name: str, *, maximum: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > maximum
    ):
        raise ReviewValidationError(f"{field_name} must be non-empty text")
    return value


def _require_oid(value: object, field_name: str) -> str:
    text = _require_text(value, field_name, maximum=64).lower()
    if not (_HEX_40.fullmatch(text) or _HEX_64.fullmatch(text)):
        raise ReviewValidationError(f"{field_name} must be a full Git OID")
    return text


def _require_digest(value: object, field_name: str) -> str:
    text = _require_text(value, field_name, maximum=64).lower()
    if not _HEX_64.fullmatch(text):
        raise ReviewValidationError(f"{field_name} must be a SHA-256 digest")
    return text


def _require_nonnegative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReviewValidationError(f"{field_name} must be a non-negative integer")
    return value


def _require_positive_int(value: object, field_name: str) -> int:
    value = _require_nonnegative_int(value, field_name)
    if value == 0:
        raise ReviewValidationError(f"{field_name} must be a positive integer")
    return value


def _require_relative_path(value: object, field_name: str = "path") -> str:
    text = _require_text(value, field_name, maximum=4096)
    if "\\" in text or text.startswith("/") or text.endswith("/"):
        raise ReviewValidationError(f"{field_name} must be a relative file path")
    path = PurePosixPath(text)
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ReviewValidationError(f"{field_name} must be a relative file path")
    return path.as_posix()


def _require_lease_path(value: object) -> str:
    text = _require_text(value, "approved lease path", maximum=4096)
    prefix = text.endswith("/")
    base = text[:-1] if prefix else text
    normalized = _require_relative_path(base, "approved lease path")
    return normalized + "/" if prefix else normalized


def _strict_json_object(raw: object, field_name: str) -> dict[str, object]:
    if not isinstance(raw, str):
        raise ReviewValidationError(f"{field_name} must be one JSON object")

    def reject_constant(value: str) -> None:
        raise ReviewValidationError(f"{field_name} contains {value}")

    def reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ReviewValidationError(f"{field_name} has a duplicate field")
            result[key] = value
        return result

    try:
        decoded = json.loads(
            raw,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_pairs,
        )
    except ReviewValidationError:
        raise
    except (TypeError, json.JSONDecodeError, RecursionError) as exc:
        raise ReviewValidationError(f"{field_name} must be one JSON object") from exc
    if not isinstance(decoded, dict):
        raise ReviewValidationError(f"{field_name} must be one JSON object")
    return decoded


def _bounded_canonical_json(
    value: object,
    field_name: str,
    *,
    expected_type: type | tuple[type, ...] | None = None,
) -> str:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError, RecursionError) as exc:
            raise ReviewValidationError(
                f"{field_name} must be valid JSON"
            ) from exc
    else:
        decoded = value
    if expected_type is not None and not isinstance(decoded, expected_type):
        raise ReviewValidationError(f"{field_name} has an invalid JSON shape")
    canonical = _canonical_json(decoded)
    if len(canonical.encode("utf-8")) > _MAX_RECOVERY_METADATA_BYTES:
        raise ReviewValidationError(f"{field_name} is oversized")
    return canonical


def _recovery_metadata_json(
    value: object,
    field_name: str,
    *,
    execution_protocol: int,
) -> str:
    def contains_callback(item: object, seen: set[int]) -> bool:
        if callable(item):
            return True
        if isinstance(item, (str, bytes, int, float, bool, type(None), Path)):
            return False
        identity = id(item)
        if identity in seen:
            return False
        seen.add(identity)
        if isinstance(item, Mapping):
            return any(
                contains_callback(key, seen) or contains_callback(child, seen)
                for key, child in item.items()
            )
        if isinstance(item, (list, tuple, set, frozenset)):
            return any(contains_callback(child, seen) for child in item)
        return False

    if contains_callback(value, set()):
        raise ReviewValidationError(
            f"{field_name} must not contain an in-memory callback"
        )
    try:
        from agent.bestplan_state import sanitize_runtime_metadata

        sanitized = sanitize_runtime_metadata(
            value,
            execution_protocol=execution_protocol,
        )
    except Exception as exc:
        raise ReviewValidationError(
            f"{field_name} contains invalid recovery metadata"
        ) from exc
    expected_type: type = list if execution_protocol == 2 else dict
    return _bounded_canonical_json(
        sanitized,
        field_name,
        expected_type=expected_type,
    )


@dataclass(frozen=True)
class ReviewTarget:
    """Tagged immutable identity of a BestPlan integration or manual snapshot."""

    source_kind: str
    generation: int
    canonical_json: str = field(repr=False)
    target_digest: str
    plan_id: str = ""
    job_id: str = ""
    repository_id: str = ""
    base_oid: str = ""
    local_target_oid: str = ""
    integration_oid: str = ""
    integration_tree_oid: str = ""
    integration_ref: str = ""
    integration_receipt_digest: str = ""
    check_receipt_digest: str = ""
    approval_digest: str = ""
    contract_digest: str = ""
    diff_sha256: str = ""
    acceptance_digest: str = ""
    policy_digest: str = ""
    snapshot_tree_oid: str = ""
    snapshot_digest: str = ""

    @classmethod
    def bestplan_integration(
        cls,
        *,
        plan_id: str,
        generation: int,
        base_oid: str,
        local_target_oid: str,
        integration_oid: str,
        integration_tree_oid: str,
        integration_ref: str,
        integration_receipt_digest: str,
        check_receipt_digest: str,
        approval_digest: str,
        contract_digest: str,
        diff_sha256: str,
        acceptance_digest: str,
        policy_digest: str,
    ) -> "ReviewTarget":
        generation = _require_nonnegative_int(generation, "generation")
        integration_ref = _require_text(
            integration_ref, "integration_ref", maximum=1024
        )
        if not integration_ref.startswith("refs/hermes-bestplan-integrations/"):
            raise ReviewValidationError("integration_ref is not BestPlan-owned")
        payload = {
            "acceptance_digest": _require_digest(
                acceptance_digest, "acceptance_digest"
            ),
            "approval_digest": _require_digest(approval_digest, "approval_digest"),
            "base_oid": _require_oid(base_oid, "base_oid"),
            "check_receipt_digest": _require_digest(
                check_receipt_digest, "check_receipt_digest"
            ),
            "contract_digest": _require_digest(contract_digest, "contract_digest"),
            "diff_sha256": _require_digest(diff_sha256, "diff_sha256"),
            "generation": generation,
            "integration_oid": _require_oid(integration_oid, "integration_oid"),
            "integration_ref": integration_ref,
            "integration_receipt_digest": _require_digest(
                integration_receipt_digest, "integration_receipt_digest"
            ),
            "integration_tree_oid": _require_oid(
                integration_tree_oid, "integration_tree_oid"
            ),
            "local_target_oid": _require_oid(local_target_oid, "local_target_oid"),
            "plan_id": _require_text(plan_id, "plan_id", maximum=256),
            "policy_digest": _require_digest(policy_digest, "policy_digest"),
            "source_kind": "bestplan_integration",
        }
        canonical = _canonical_json(payload)
        return cls(
            **payload,
            canonical_json=canonical,
            target_digest=_domain_digest(_TARGET_DOMAIN, canonical),
        )

    @classmethod
    def manual_snapshot(
        cls,
        *,
        job_id: str,
        generation: int,
        repository_id: str,
        base_oid: str,
        snapshot_tree_oid: str,
        snapshot_digest: str,
        diff_sha256: str,
        acceptance_digest: str,
        policy_digest: str,
        check_receipt_digest: str = _MANUAL_ZERO_DIGEST,
    ) -> "ReviewTarget":
        generation = _require_nonnegative_int(generation, "generation")
        payload = {
            "acceptance_digest": _require_digest(
                acceptance_digest, "acceptance_digest"
            ),
            "base_oid": _require_oid(base_oid, "base_oid"),
            "check_receipt_digest": _require_digest(
                check_receipt_digest, "check_receipt_digest"
            ),
            "diff_sha256": _require_digest(diff_sha256, "diff_sha256"),
            "generation": generation,
            "job_id": _require_text(job_id, "job_id", maximum=256),
            "policy_digest": _require_digest(policy_digest, "policy_digest"),
            "repository_id": _require_text(
                repository_id, "repository_id", maximum=512
            ),
            "snapshot_digest": _require_digest(snapshot_digest, "snapshot_digest"),
            "snapshot_tree_oid": _require_oid(
                snapshot_tree_oid, "snapshot_tree_oid"
            ),
            "source_kind": "manual_snapshot",
        }
        canonical = _canonical_json(payload)
        return cls(
            source_kind="manual_snapshot",
            generation=generation,
            canonical_json=canonical,
            target_digest=_domain_digest(_TARGET_DOMAIN, canonical),
            plan_id=payload["job_id"],
            job_id=payload["job_id"],
            repository_id=payload["repository_id"],
            base_oid=payload["base_oid"],
            local_target_oid=payload["base_oid"],
            integration_oid=payload["snapshot_tree_oid"],
            integration_tree_oid=payload["snapshot_tree_oid"],
            check_receipt_digest=payload["check_receipt_digest"],
            diff_sha256=payload["diff_sha256"],
            acceptance_digest=payload["acceptance_digest"],
            policy_digest=payload["policy_digest"],
            snapshot_tree_oid=payload["snapshot_tree_oid"],
            snapshot_digest=payload["snapshot_digest"],
        )

    @classmethod
    def build(
        cls,
        *,
        plan_id: str,
        generation: int,
        base_oid: str,
        local_target_oid: str,
        integration_oid: str,
        integration_tree_oid: str,
        integration_receipt_digest: str,
        check_receipt_digest: str,
        approval_digest: str,
        contract_digest: str,
        diff_sha256: str,
        policy_digest: str,
        integration_ref: str | None = None,
        acceptance_digest: str | None = None,
    ) -> "ReviewTarget":
        return cls.bestplan_integration(
            plan_id=plan_id,
            generation=generation,
            base_oid=base_oid,
            local_target_oid=local_target_oid,
            integration_oid=integration_oid,
            integration_tree_oid=integration_tree_oid,
            integration_ref=(
                integration_ref
                or f"refs/hermes-bestplan-integrations/{plan_id}/{generation}"
            ),
            integration_receipt_digest=integration_receipt_digest,
            check_receipt_digest=check_receipt_digest,
            approval_digest=approval_digest,
            contract_digest=contract_digest,
            diff_sha256=diff_sha256,
            acceptance_digest=acceptance_digest or contract_digest,
            policy_digest=policy_digest,
        )


def attach_manual_target(*, active_bestplan_target: ReviewTarget) -> ReviewTarget:
    if (
        not isinstance(active_bestplan_target, ReviewTarget)
        or active_bestplan_target.source_kind != "bestplan_integration"
    ):
        raise ReviewValidationError("manual attachment requires an active BestPlan target")
    return active_bestplan_target


def _restore_review_target(canonical_json: str) -> ReviewTarget:
    value = _strict_json_object(canonical_json, "stored review target")
    source_kind = value.get("source_kind")
    if source_kind == "bestplan_integration":
        target = ReviewTarget.bestplan_integration(
            **{key: item for key, item in value.items() if key != "source_kind"}
        )
    elif source_kind == "manual_snapshot":
        target = ReviewTarget.manual_snapshot(
            **{key: item for key, item in value.items() if key != "source_kind"}
        )
    else:
        raise ReviewStoreConflict("stored review target kind is invalid")
    if not hmac.compare_digest(target.canonical_json, canonical_json):
        raise ReviewStoreConflict("stored review target bytes conflict")
    return target


@dataclass(frozen=True)
class ReviewArtifact:
    """Immutable, bounded review material supplied by a host adapter."""

    target_digest: str
    canonical_json: str = field(repr=False)
    artifact_digest: str

    @classmethod
    def build(
        cls,
        *,
        target: ReviewTarget,
        diff_bytes: bytes,
        task: str,
        acceptance: Sequence[str],
        rules: Sequence[str],
        issue_locator_catalog: Mapping[str, Mapping[str, object]],
        dispositions: Sequence[Mapping[str, object]],
    ) -> "ReviewArtifact":
        if not isinstance(target, ReviewTarget):
            raise ReviewValidationError("review artifact target is invalid")
        if not isinstance(diff_bytes, bytes) or len(diff_bytes) > _MAX_REVIEW_DIFF_BYTES:
            raise ReviewValidationError("review artifact diff bytes are invalid")
        diff_sha256 = hashlib.sha256(diff_bytes).hexdigest()
        if diff_sha256 != target.diff_sha256:
            raise ReviewValidationError("review artifact diff differs from its target")
        task = _require_text(
            task, "review task", maximum=_MAX_REVIEW_ARTIFACT_FIELD_BYTES
        )

        def text_list(value: object, field_name: str) -> list[str]:
            if (
                not isinstance(value, Sequence)
                or isinstance(value, (str, bytes, bytearray))
                or not value
                or len(value) > _MAX_REVIEW_LIST_ITEMS
            ):
                raise ReviewValidationError(f"{field_name} must be a bounded list")
            return [
                _require_text(
                    item,
                    field_name,
                    maximum=_MAX_REVIEW_ARTIFACT_FIELD_BYTES,
                )
                for item in value
            ]

        acceptance_items = text_list(acceptance, "review acceptance")
        rule_items = text_list(rules, "review rule")
        if (
            not isinstance(issue_locator_catalog, Mapping)
            or len(issue_locator_catalog) > _MAX_REVIEW_LIST_ITEMS
        ):
            raise ReviewValidationError("review issue locator catalog is invalid")
        catalog: dict[str, dict[str, str]] = {}
        for raw_locator_id, raw_entry in issue_locator_catalog.items():
            locator_id = _require_text(
                raw_locator_id, "review issue locator ID", maximum=256
            )
            if not isinstance(raw_entry, Mapping):
                raise ReviewValidationError("review issue locator entry is invalid")
            entry_fields = set(raw_entry)
            if not {"kind", "identifier"}.issubset(entry_fields) or not (
                entry_fields <= {"kind", "identifier", "quoted_evidence"}
            ):
                raise ReviewValidationError("review issue locator entry is invalid")
            kind = _require_text(raw_entry["kind"], "review locator kind", maximum=64)
            if kind not in _SUPPORTED_LOCATOR_KINDS:
                raise ReviewValidationError("review issue locator kind is unsupported")
            catalog_entry = {
                "identifier": _require_text(
                    raw_entry["identifier"],
                    "review issue locator identifier",
                    maximum=4096,
                ),
                "kind": kind,
            }
            if "quoted_evidence" in raw_entry:
                if kind not in {"unchanged_dependency", "contract_or_receipt"}:
                    raise ReviewValidationError(
                        "review issue locator evidence is unsupported"
                    )
                catalog_entry["quoted_evidence"] = _require_text(
                    raw_entry["quoted_evidence"],
                    "review issue locator evidence",
                    maximum=_MAX_REVIEW_ARTIFACT_FIELD_BYTES,
                )
            catalog[locator_id] = catalog_entry
        if (
            not isinstance(dispositions, Sequence)
            or isinstance(dispositions, (str, bytes, bytearray))
            or len(dispositions) > _MAX_FINDINGS
        ):
            raise ReviewValidationError("review dispositions are invalid")
        disposition_items: list[dict[str, str]] = []
        for raw_disposition in dispositions:
            if not isinstance(raw_disposition, Mapping) or set(raw_disposition) != {
                "evidence",
                "finding_fingerprint",
                "status",
            }:
                raise ReviewValidationError("review disposition is invalid")
            status = _require_text(
                raw_disposition["status"], "review disposition status", maximum=32
            )
            if status not in {"fixed", "disputed"}:
                raise ReviewValidationError("review disposition status is unsupported")
            disposition_items.append(
                {
                    "evidence": _require_text(
                        raw_disposition["evidence"],
                        "review disposition evidence",
                        maximum=_MAX_REVIEW_ARTIFACT_FIELD_BYTES,
                    ),
                    "finding_fingerprint": _require_digest(
                        raw_disposition["finding_fingerprint"],
                        "review finding fingerprint",
                    ),
                    "status": status,
                }
            )
        try:
            diff_text: str | None = diff_bytes.decode("utf-8")
        except UnicodeDecodeError:
            diff_text = None
        payload = {
            "acceptance": acceptance_items,
            "dispositions": disposition_items,
            "git_diff": {
                "content_base64": base64.b64encode(diff_bytes).decode("ascii"),
                "sha256": diff_sha256,
                "text": diff_text,
            },
            "issue_locator_catalog": catalog,
            "rules": rule_items,
            "schema": "hermes.bestplan.review-artifact.v1",
            "target_digest": target.target_digest,
            "task": task,
        }
        canonical = _canonical_json(payload)
        return cls(
            target_digest=target.target_digest,
            canonical_json=canonical,
            artifact_digest=_domain_digest(
                b"hermes.bestplan.review-artifact.v1\0", canonical
            ),
        )


def _restore_review_artifact(
    canonical_json: str,
    *,
    target: ReviewTarget,
) -> ReviewArtifact:
    value = _strict_json_object(canonical_json, "stored review artifact")
    if set(value) != {
        "acceptance",
        "dispositions",
        "git_diff",
        "issue_locator_catalog",
        "rules",
        "schema",
        "target_digest",
        "task",
    } or value.get("schema") != "hermes.bestplan.review-artifact.v1":
        raise ReviewStoreConflict("stored review artifact is invalid")
    diff = value.get("git_diff")
    if not isinstance(diff, dict) or set(diff) != {
        "content_base64",
        "sha256",
        "text",
    }:
        raise ReviewStoreConflict("stored review diff is invalid")
    try:
        diff_bytes = base64.b64decode(diff["content_base64"], validate=True)
    except (TypeError, ValueError) as exc:
        raise ReviewStoreConflict("stored review diff is invalid") from exc
    artifact = ReviewArtifact.build(
        target=target,
        diff_bytes=diff_bytes,
        task=value["task"],
        acceptance=value["acceptance"],
        rules=value["rules"],
        issue_locator_catalog=value["issue_locator_catalog"],
        dispositions=value["dispositions"],
    )
    if not hmac.compare_digest(artifact.canonical_json, canonical_json):
        raise ReviewStoreConflict("stored review artifact bytes conflict")
    return artifact


def build_review_packet(
    target: ReviewTarget,
    *,
    artifact: ReviewArtifact | None = None,
) -> str:
    if not isinstance(target, ReviewTarget):
        raise ReviewValidationError("review target is invalid")
    packet: dict[str, object] = {
        "schema": "hermes.bestplan.review-request.v1",
        "target": json.loads(target.canonical_json),
        "target_digest": target.target_digest,
    }
    if artifact is not None:
        if (
            not isinstance(artifact, ReviewArtifact)
            or artifact.target_digest != target.target_digest
        ):
            raise ReviewValidationError("review artifact differs from its target")
        packet["artifact"] = json.loads(artifact.canonical_json)
        packet["artifact_digest"] = artifact.artifact_digest
    canonical = _canonical_json(packet)
    if len(canonical.encode("utf-8")) > REVIEW_PACKET_MAX_BYTES:
        raise ReviewValidationError("final review packet is too large")
    return canonical


@dataclass(frozen=True)
class _ManualOwner:
    session_id: str
    profile: str
    workspace: Path
    state_db_path: Path


@dataclass(frozen=True)
class _ManualCapture:
    base_oid: str
    base_tree_oid: str
    snapshot_tree_oid: str
    diff_bytes: bytes
    changed_paths: tuple[str, ...]
    live_state_digest: str
    repository_control_digest: str
    path_states: tuple[Mapping[str, object], ...]
    after_blobs: Mapping[str, bytes]
    before_blobs: Mapping[str, bytes]
    changed_lines: Mapping[str, frozenset[int]]
    deleted_lines: Mapping[str, frozenset[int]]
    deleted_paths: tuple[str, ...]


@dataclass(frozen=True)
class _ManualReviewBundle:
    target: ReviewTarget
    artifact: ReviewArtifact
    evidence: "EvidenceContext"
    capture: _ManualCapture
    manifest_path: Path
    adapter_state: Mapping[str, object]


@dataclass(frozen=True)
class _ActiveBestplanAttachment:
    job: "ReviewJob"
    generation: "ReviewGeneration"
    target: ReviewTarget
    artifact: ReviewArtifact
    evidence: "EvidenceContext"


class _DefaultManualReviewRuntime:
    """Resolve live reviewer credentials without making them snapshot authority."""

    adapter_version = _MANUAL_ADAPTER_VERSION

    def __init__(self, agent: object, workspace: Path):
        self.agent = agent
        self.workspace = workspace
        self._authority_bindings: tuple[object, object] | None = None
        self._reviewer_bindings: tuple[ReviewerBinding, ReviewerBinding] | None = None
        self._check_results: dict[
            tuple[int, tuple[str, ...]], Mapping[str, object]
        ] = {}

    def _resolve_reviewers(self) -> None:
        if self._authority_bindings is not None:
            return
        from agent.bestplan_local import build_local_review_authority_bindings
        from agent.bestplan_state import _local_review_runtime_tasks
        from tools.delegate_tool import resolve_bestplan_runtime_specs

        tasks = _local_review_runtime_tasks(str(self.workspace))
        runtimes = resolve_bestplan_runtime_specs(
            tasks,
            self.agent,
            execution_protocol=2,
        )
        authorities = build_local_review_authority_bindings(runtimes)
        self._authority_bindings = authorities
        self._reviewer_bindings = tuple(
            ReviewerBinding(
                slot=item.slot,
                provider=item.provider,
                model=item.model,
                model_family=item.model_family,
            )
            for item in authorities
        )  # type: ignore[assignment]

    @property
    def reviewer_bindings(self) -> tuple[ReviewerBinding, ReviewerBinding]:
        self._resolve_reviewers()
        assert self._reviewer_bindings is not None
        return self._reviewer_bindings

    def reviewer_call(
        self,
        binding: ReviewerBinding,
        request: dict[str, object],
    ) -> str:
        from agent.bestplan_local import call_local_review_authority

        self._resolve_reviewers()
        assert self._authority_bindings is not None
        by_slot = {item.slot: item for item in self._authority_bindings}
        authority = by_slot.get(binding.slot)
        if authority is None:
            raise ReviewValidationError("manual reviewer authority is unavailable")
        return call_local_review_authority(authority, request)

    def refresh_reviewers(self) -> None:
        from agent.bestplan_local import refresh_local_review_authority_bindings

        self._resolve_reviewers()
        assert self._authority_bindings is not None
        authorities = refresh_local_review_authority_bindings(
            self._authority_bindings  # type: ignore[arg-type]
        )
        self._authority_bindings = authorities
        self._reviewer_bindings = tuple(
            ReviewerBinding(
                slot=item.slot,
                provider=item.provider,
                model=item.model,
                model_family=item.model_family,
            )
            for item in authorities
        )  # type: ignore[assignment]

    def repair_generation(self, **kwargs: object) -> Mapping[str, object]:
        from agent.manual_review_runtime import execute_manual_bestplan_repair

        result = execute_manual_bestplan_repair(agent=self.agent, **kwargs)
        if not isinstance(result, Mapping):
            return {
                "status": "waiting",
                "reason": "manual review repair runtime returned invalid evidence",
            }
        if str(result.get("status") or "").strip().casefold() != "applied":
            return result
        try:
            generation = _require_nonnegative_int(
                kwargs.get("generation"), "manual repair generation"
            )
            changed_paths = tuple(sorted(
                _require_relative_path(item, "manual repair changed path")
                for item in result.get("changed_paths", ())
            ))
            receipt_digest = _require_digest(
                result.get("check_receipt_digest"),
                "manual repair check receipt",
            )
        except (ReviewValidationError, TypeError):
            return {
                "status": "waiting",
                "reason": "manual review repair runtime returned invalid evidence",
            }
        self._check_results[(generation, changed_paths)] = {
            "status": "passed",
            "receipt_digest": receipt_digest,
        }
        return result

    def run_checks(self, **kwargs: object) -> Mapping[str, object]:
        try:
            generation = _require_nonnegative_int(
                kwargs.get("generation"), "manual check generation"
            )
            changed_paths = tuple(sorted(
                _require_relative_path(item, "manual check changed path")
                for item in kwargs.get("changed_paths", ())
            ))
        except (ReviewValidationError, TypeError):
            return {
                "status": "waiting",
                "reason": "manual review check request is invalid",
            }
        result = self._check_results.pop((generation, changed_paths), None)
        if result is not None:
            return result
        return {
            "status": "waiting",
            "reason": "manual review has no matching frozen check receipt",
        }


def _manual_git(
    workspace: Path,
    *args: str,
    input_bytes: bytes | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> bytes:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_PAGER": "cat",
            "LC_ALL": "C",
        }
    )
    if extra_env:
        environment.update(extra_env)
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=workspace,
            env=environment,
            input=input_bytes,
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReviewValidationError("manual review Git evidence is unavailable") from exc
    return bytes(result.stdout)


def _manual_owner(agent: object) -> _ManualOwner:
    session_id = _require_text(
        getattr(agent, "session_id", None),
        "manual review session",
        maximum=512,
    )
    database = getattr(agent, "_session_db", None)
    if database is None:
        getter = getattr(agent, "_get_session_db_for_recall", None)
        database = getter() if callable(getter) else None
    row = database.get_session(session_id) if database is not None else None
    if row is not None and not isinstance(row, Mapping):
        raise ReviewValidationError("manual review session metadata is invalid")
    raw_workspace = (
        (row or {}).get("git_repo_root")
        or (row or {}).get("cwd")
    )
    if not raw_workspace:
        from agent.runtime_cwd import resolve_agent_cwd

        raw_workspace = str(resolve_agent_cwd())
    try:
        requested = Path(str(raw_workspace)).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ReviewValidationError("manual review workspace is unavailable") from exc
    root_raw = _manual_git(requested, "rev-parse", "--show-toplevel").strip()
    try:
        workspace = Path(root_raw.decode("utf-8", "strict")).resolve(strict=True)
    except (UnicodeError, OSError, RuntimeError) as exc:
        raise ReviewValidationError("manual review repository identity is invalid") from exc
    profile = str((row or {}).get("profile_name") or "").strip()
    if not profile:
        from hermes_cli.profiles import get_active_profile_name

        profile = get_active_profile_name() or "default"
    profile = _require_text(profile, "manual review profile", maximum=128)
    state_db = getattr(database, "db_path", None)
    if state_db is None:
        from hermes_constants import get_hermes_home

        state_db = get_hermes_home() / "state.db"
    return _ManualOwner(
        session_id=session_id,
        profile=profile,
        workspace=workspace,
        state_db_path=Path(state_db).expanduser().resolve(),
    )


def _manual_relative_path(workspace: Path, raw: object) -> tuple[str, bool]:
    text = _require_text(raw, "manual review path", maximum=4096)
    directory_scope = text.endswith("/")
    candidate = Path(text).expanduser()
    if candidate.is_absolute():
        try:
            text = candidate.resolve(strict=False).relative_to(workspace).as_posix()
        except (OSError, RuntimeError, ValueError) as exc:
            raise ReviewRequiresAuthority(
                "manual review path is outside the owning workspace"
            ) from exc
    else:
        text = text.rstrip("/")
    return _require_relative_path(text, "manual review path"), directory_scope


def _manual_status_records(workspace: Path) -> dict[str, str]:
    output = _manual_git(
        workspace,
        "-c",
        "status.renames=false",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--no-renames",
    )
    records: dict[str, str] = {}
    for raw in output.split(b"\0"):
        if not raw:
            continue
        if len(raw) < 4 or raw[2:3] != b" ":
            raise ReviewValidationError("manual review Git status is malformed")
        try:
            path = raw[3:].decode("utf-8", "strict")
            status_text = raw[:2].decode("ascii", "strict")
        except UnicodeError as exc:
            raise ReviewValidationError(
                "manual review changed path is not UTF-8"
            ) from exc
        normalized = _require_relative_path(path, "manual review changed path")
        if normalized in records:
            raise ReviewValidationError("manual review changed paths are ambiguous")
        records[normalized] = status_text
    if len(records) > _MANUAL_SNAPSHOT_MAX_FILES:
        raise ReviewValidationError("manual review has too many changed files")
    return records


def _manual_objective_paths(
    *,
    owner: _ManualOwner,
    scope: str,
) -> tuple[str, ...]:
    if not isinstance(scope, str) or "\x00" in scope or len(scope.encode()) > 16_384:
        raise ReviewValidationError("manual review scope is invalid")
    status = _manual_status_records(owner.workspace)
    requested: list[tuple[str, bool]] = []
    if scope.strip():
        try:
            tokens = shlex.split(scope)
        except ValueError as exc:
            raise ReviewValidationError("manual review scope is malformed") from exc
        if not tokens:
            raise ReviewValidationError("manual review scope is malformed")
        requested = [
            _manual_relative_path(owner.workspace, token) for token in tokens
        ]
    else:
        from agent.verification_evidence import verification_status

        evidence = verification_status(
            session_id=owner.session_id,
            cwd=owner.workspace,
        )
        raw_paths = evidence.get("changed_paths") if isinstance(evidence, Mapping) else None
        if isinstance(raw_paths, list):
            requested = [
                _manual_relative_path(owner.workspace, item) for item in raw_paths
            ]
    selected: set[str] = set()
    for path, directory_scope in requested:
        for changed in status:
            if changed == path or (directory_scope and changed.startswith(path + "/")):
                selected.add(changed)
    return tuple(sorted(selected))


def _manual_file_identity(
    info: os.stat_result,
) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _manual_parent_identity(info: os.stat_result) -> tuple[int, int, int]:
    return (info.st_dev, info.st_ino, info.st_mode)


@dataclass(frozen=True)
class _ManualParentDescriptor:
    root: Path
    descriptors: tuple[int, ...]
    components: tuple[str, ...]

    @property
    def parent_fd(self) -> int:
        return self.descriptors[-1]

    def close(self) -> None:
        for descriptor in reversed(self.descriptors):
            os.close(descriptor)


def _manual_revalidate_parent(parent: _ManualParentDescriptor) -> None:
    try:
        rebound_root = os.stat(parent.root, follow_symlinks=False)
        opened_root = os.fstat(parent.descriptors[0])
        if (
            stat.S_ISLNK(rebound_root.st_mode)
            or not stat.S_ISDIR(rebound_root.st_mode)
            or not stat.S_ISDIR(opened_root.st_mode)
            or _manual_parent_identity(rebound_root)
            != _manual_parent_identity(opened_root)
        ):
            raise ReviewValidationError("manual review path parent changed")
        for index, component in enumerate(parent.components):
            rebound = os.stat(
                component,
                dir_fd=parent.descriptors[index],
                follow_symlinks=False,
            )
            opened = os.fstat(parent.descriptors[index + 1])
            if (
                stat.S_ISLNK(rebound.st_mode)
                or not stat.S_ISDIR(rebound.st_mode)
                or not stat.S_ISDIR(opened.st_mode)
                or _manual_parent_identity(rebound)
                != _manual_parent_identity(opened)
            ):
                raise ReviewValidationError("manual review path parent changed")
    except ReviewValidationError:
        raise
    except OSError as exc:
        raise ReviewValidationError("manual review path parent changed") from exc


def _manual_open_parent_descriptor(
    workspace: Path,
    path: str,
) -> _ManualParentDescriptor | None:
    try:
        resolved_workspace = workspace.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ReviewValidationError(
            "manual review path parent is unavailable"
        ) from exc
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before = os.stat(resolved_workspace, follow_symlinks=False)
        root_fd = os.open(resolved_workspace, flags)
    except OSError as exc:
        raise ReviewValidationError(
            "manual review path parent is unavailable"
        ) from exc
    try:
        opened = os.fstat(root_fd)
    except OSError as exc:
        os.close(root_fd)
        raise ReviewValidationError("manual review path parent changed") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISDIR(before.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or _manual_parent_identity(before) != _manual_parent_identity(opened)
    ):
        os.close(root_fd)
        raise ReviewValidationError("manual review path parent changed")
    descriptors = [root_fd]
    components: list[str] = []
    try:
        for component in PurePosixPath(path).parts[:-1]:
            try:
                entry = os.stat(
                    component,
                    dir_fd=descriptors[-1],
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                parent = _ManualParentDescriptor(
                    root=resolved_workspace,
                    descriptors=tuple(descriptors),
                    components=tuple(components),
                )
                _manual_revalidate_parent(parent)
                try:
                    os.stat(
                        component,
                        dir_fd=descriptors[-1],
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    parent.close()
                    return None
                raise ReviewValidationError(
                    "manual review path parent changed"
                )
            if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
                raise ReviewValidationError(
                    "manual review path parent is unsafe"
                )
            child_fd = -1
            try:
                child_fd = os.open(component, flags, dir_fd=descriptors[-1])
                opened_child = os.fstat(child_fd)
            except OSError as exc:
                if child_fd >= 0:
                    os.close(child_fd)
                raise ReviewValidationError(
                    "manual review path parent changed"
                ) from exc
            if (
                not stat.S_ISDIR(opened_child.st_mode)
                or _manual_parent_identity(entry)
                != _manual_parent_identity(opened_child)
            ):
                os.close(child_fd)
                raise ReviewValidationError(
                    "manual review path parent changed"
                )
            components.append(component)
            descriptors.append(child_fd)
        return _ManualParentDescriptor(
            root=resolved_workspace,
            descriptors=tuple(descriptors),
            components=tuple(components),
        )
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def _manual_path_state_and_bytes(
    workspace: Path,
    path: str,
) -> tuple[dict[str, object], bytes | None]:
    path = _require_relative_path(path, "manual review path")
    parent = _manual_open_parent_descriptor(workspace, path)
    if parent is None:
        return {"kind": "absent", "path": path}, None
    parent_fd = parent.parent_fd
    target_name = PurePosixPath(path).name
    try:
        try:
            before = os.stat(
                target_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            _manual_revalidate_parent(parent)
            try:
                os.stat(
                    target_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return {"kind": "absent", "path": path}, None
            raise ReviewValidationError("manual review file changed during capture")
        except OSError as exc:
            raise ReviewValidationError(
                "manual review file is unavailable"
            ) from exc
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ReviewValidationError("manual review supports only regular files")
        if before.st_size > _MANUAL_SNAPSHOT_MAX_BYTES:
            raise ReviewValidationError("manual review file exceeds the size limit")

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(target_name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise ReviewValidationError(
                "manual review file changed during capture"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or _manual_file_identity(before) != _manual_file_identity(opened)
            ):
                raise ReviewValidationError(
                    "manual review file changed during capture"
                )
            data = bytearray()
            while len(data) <= _MANUAL_SNAPSHOT_MAX_BYTES:
                chunk = os.read(
                    descriptor,
                    min(
                        1024 * 1024,
                        _MANUAL_SNAPSHOT_MAX_BYTES + 1 - len(data),
                    ),
                )
                if not chunk:
                    break
                data.extend(chunk)
            after_open = os.fstat(descriptor)
        except OSError as exc:
            raise ReviewValidationError(
                "manual review file changed during capture"
            ) from exc
        finally:
            os.close(descriptor)
        try:
            after_path = os.stat(
                target_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ReviewValidationError(
                "manual review file changed during capture"
            ) from exc
        _manual_revalidate_parent(parent)
        if len(data) > _MANUAL_SNAPSHOT_MAX_BYTES or not (
            _manual_file_identity(before)
            == _manual_file_identity(opened)
            == _manual_file_identity(after_open)
            == _manual_file_identity(after_path)
        ):
            raise ReviewValidationError("manual review file changed during capture")
        frozen = bytes(data)
        return (
            {
                "kind": "file",
                "mode": "100755" if opened.st_mode & stat.S_IXUSR else "100644",
                "path": path,
                "sha256": hashlib.sha256(frozen).hexdigest(),
                "size": len(frozen),
            },
            frozen,
        )
    finally:
        parent.close()


def _manual_path_state(workspace: Path, path: str) -> dict[str, object]:
    state, _data = _manual_path_state_and_bytes(workspace, path)
    return state


def _manual_live_state_digest(workspace: Path, paths: Sequence[str]) -> str:
    states = [_manual_path_state(workspace, path) for path in paths]
    return _domain_digest(
        b"hermes.manual-review-live-state.v1\0",
        _canonical_json(states),
    )


def _manual_repository_control_digest(workspace: Path) -> str:
    head = _manual_git(workspace, "rev-parse", "HEAD^{commit}").strip().decode(
        "ascii"
    )
    index = _manual_git(workspace, "ls-files", "--stage", "-z")
    return _domain_digest(
        b"hermes.manual-review-repository-control.v1\0",
        _canonical_json(
            {
                "head_oid": _require_oid(head, "manual review control HEAD"),
                "index_sha256": hashlib.sha256(index).hexdigest(),
            }
        ),
    )


def _manual_ambient_digest(workspace: Path, allowed_paths: Sequence[str]) -> str:
    allowed = set(allowed_paths)
    status = _manual_status_records(workspace)
    payload = {
        "repository_control_digest": _manual_repository_control_digest(workspace),
        "unowned_changes": [
            {
                "path_state": _manual_path_state(workspace, path),
                "status": code,
            }
            for path, code in sorted(status.items())
            if path not in allowed
        ],
    }
    return _domain_digest(
        b"hermes.manual-review-ambient.v1\0",
        _canonical_json(payload),
    )


def _manual_blob_at(workspace: Path, treeish: str, path: str) -> bytes:
    listing = _manual_git(
        workspace,
        "ls-tree",
        "-z",
        "--full-tree",
        treeish,
        "--",
        path,
    )
    expected = path.encode("utf-8")
    for record in listing.split(b"\0"):
        if not record or b"\t" not in record:
            continue
        header, listed_path = record.split(b"\t", 1)
        if listed_path != expected:
            continue
        fields = header.split()
        if len(fields) != 3 or fields[1] != b"blob":
            raise ReviewValidationError("manual review tree entry is not a file")
        data = _manual_git(workspace, "cat-file", "blob", fields[2].decode("ascii"))
        if len(data) > _MANUAL_SNAPSHOT_MAX_BYTES:
            raise ReviewValidationError("manual review blob exceeds the size limit")
        return data
    raise FileNotFoundError(path)


def _manual_line_sets(before: bytes, after: bytes) -> tuple[frozenset[int], frozenset[int]]:
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    changed: set[int] = set()
    deleted: set[int] = set()
    matcher = difflib.SequenceMatcher(None, before_lines, after_lines, autojunk=False)
    for tag, first_start, first_end, second_start, second_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        deleted.update(range(first_start + 1, first_end + 1))
        changed.update(range(second_start + 1, second_end + 1))
    return frozenset(changed), frozenset(deleted)


def _capture_manual_snapshot(
    workspace: Path,
    changed_paths: Sequence[str],
) -> _ManualCapture:
    paths = tuple(_require_relative_path(item) for item in changed_paths)
    if not paths or len(set(paths)) != len(paths):
        raise ReviewNoTarget("manual review has no objective artifact")
    repository_control_digest = _manual_repository_control_digest(workspace)
    base_oid = _manual_git(workspace, "rev-parse", "HEAD^{commit}").strip().decode("ascii")
    base_oid = _require_oid(base_oid, "manual review base OID")
    base_tree_oid = _manual_git(workspace, "rev-parse", "HEAD^{tree}").strip().decode("ascii")
    base_tree_oid = _require_oid(base_tree_oid, "manual review base tree OID")
    index_fd, index_name = tempfile.mkstemp(prefix="hermes-manual-review-index-")
    os.close(index_fd)
    os.unlink(index_name)
    index_env = {"GIT_INDEX_FILE": index_name}
    captured_states: list[dict[str, object]] = []
    try:
        _manual_git(workspace, "read-tree", base_oid, extra_env=index_env)
        for path in paths:
            state, data = _manual_path_state_and_bytes(workspace, path)
            captured_states.append(state)
            if data is None:
                _manual_git(
                    workspace,
                    "update-index",
                    "--force-remove",
                    "--",
                    path,
                    extra_env=index_env,
                )
                continue
            oid = _manual_git(
                workspace,
                "hash-object",
                "-w",
                "--stdin",
                input_bytes=data,
            ).strip().decode("ascii")
            mode = str(state["mode"])
            _manual_git(
                workspace,
                "update-index",
                "--add",
                "--cacheinfo",
                mode,
                _require_oid(oid, "manual review blob OID"),
                path,
                extra_env=index_env,
            )
        tree_oid = _manual_git(workspace, "write-tree", extra_env=index_env).strip().decode("ascii")
        tree_oid = _require_oid(tree_oid, "manual review snapshot tree OID")
    finally:
        try:
            os.unlink(index_name)
        except FileNotFoundError:
            pass
    diff_bytes = _manual_git(
        workspace,
        "diff",
        "--binary",
        "--full-index",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
        base_oid,
        tree_oid,
        "--",
        *paths,
    )
    if not diff_bytes:
        raise ReviewNoTarget("manual review has no objective artifact")
    if len(diff_bytes) > _MAX_REVIEW_DIFF_BYTES:
        raise ReviewValidationError("manual review diff exceeds the size limit")
    after: dict[str, bytes] = {}
    before: dict[str, bytes] = {}
    changed_lines: dict[str, frozenset[int]] = {}
    deleted_lines: dict[str, frozenset[int]] = {}
    deleted_paths: list[str] = []
    for path in paths:
        try:
            before_bytes = _manual_blob_at(workspace, base_oid, path)
            before[path] = before_bytes
        except FileNotFoundError:
            before_bytes = b""
        try:
            after_bytes = _manual_blob_at(workspace, tree_oid, path)
            after[path] = after_bytes
        except FileNotFoundError:
            after_bytes = b""
            if before_bytes:
                deleted_paths.append(path)
        changed, deleted = _manual_line_sets(before_bytes, after_bytes)
        changed_lines[path] = changed
        deleted_lines[path] = deleted
    return _ManualCapture(
        base_oid=base_oid,
        base_tree_oid=base_tree_oid,
        snapshot_tree_oid=tree_oid,
        diff_bytes=diff_bytes,
        changed_paths=paths,
        live_state_digest=_domain_digest(
            b"hermes.manual-review-live-state.v1\0",
            _canonical_json(captured_states),
        ),
        repository_control_digest=repository_control_digest,
        path_states=tuple(MappingProxyType(item) for item in captured_states),
        after_blobs=MappingProxyType(after),
        before_blobs=MappingProxyType(before),
        changed_lines=MappingProxyType(changed_lines),
        deleted_lines=MappingProxyType(deleted_lines),
        deleted_paths=tuple(deleted_paths),
    )


def _capture_manual_tree_snapshot(
    workspace: Path,
    changed_paths: Sequence[str],
    *,
    snapshot_tree_oid: str,
) -> _ManualCapture:
    """Build a frozen capture from a checked Git tree before live adoption."""

    paths = tuple(sorted(_require_relative_path(item) for item in changed_paths))
    if not paths or len(paths) != len(set(paths)):
        raise ReviewNoTarget("manual repair has no objective artifact")
    snapshot_tree_oid = _require_oid(
        snapshot_tree_oid, "manual repair snapshot tree"
    )
    base_oid = _require_oid(
        _manual_git(workspace, "rev-parse", "HEAD^{commit}")
        .strip()
        .decode("ascii"),
        "manual repair base OID",
    )
    base_tree_oid = _require_oid(
        _manual_git(workspace, "rev-parse", "HEAD^{tree}")
        .strip()
        .decode("ascii"),
        "manual repair base tree OID",
    )
    diff_bytes = _manual_git(
        workspace,
        "diff",
        "--binary",
        "--full-index",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
        base_oid,
        snapshot_tree_oid,
        "--",
        *paths,
    )
    if not diff_bytes or len(diff_bytes) > _MAX_REVIEW_DIFF_BYTES:
        raise ReviewValidationError("manual repair diff is invalid")
    after: dict[str, bytes] = {}
    before: dict[str, bytes] = {}
    states: list[Mapping[str, object]] = []
    changed_lines: dict[str, frozenset[int]] = {}
    deleted_lines: dict[str, frozenset[int]] = {}
    deleted_paths: list[str] = []
    for path in paths:
        try:
            before_bytes = _manual_blob_at(workspace, base_oid, path)
            before[path] = before_bytes
        except FileNotFoundError:
            before_bytes = b""
        try:
            after_bytes = _manual_blob_at(workspace, snapshot_tree_oid, path)
            mode_output = _manual_git(
                workspace,
                "ls-tree",
                snapshot_tree_oid,
                "--",
                path,
            ).split()
            if len(mode_output) < 3 or mode_output[1] != b"blob":
                raise ReviewValidationError(
                    "manual repair tree entry is invalid"
                )
            mode = mode_output[0].decode("ascii")
            if mode not in {"100644", "100755"}:
                raise ReviewValidationError(
                    "manual repair tree mode is unsupported"
                )
            after[path] = after_bytes
            states.append(
                MappingProxyType(
                    {
                        "kind": "file",
                        "mode": mode,
                        "path": path,
                        "sha256": hashlib.sha256(after_bytes).hexdigest(),
                        "size": len(after_bytes),
                    }
                )
            )
        except FileNotFoundError:
            after_bytes = b""
            states.append(MappingProxyType({"kind": "absent", "path": path}))
            if path in before:
                deleted_paths.append(path)
        changed, deleted = _manual_line_sets(before_bytes, after_bytes)
        changed_lines[path] = changed
        deleted_lines[path] = deleted
    return _ManualCapture(
        base_oid=base_oid,
        base_tree_oid=base_tree_oid,
        snapshot_tree_oid=snapshot_tree_oid,
        diff_bytes=diff_bytes,
        changed_paths=paths,
        live_state_digest=_domain_digest(
            b"hermes.manual-review-live-state.v1\0",
            _canonical_json([dict(item) for item in states]),
        ),
        repository_control_digest=_manual_repository_control_digest(workspace),
        path_states=tuple(states),
        after_blobs=MappingProxyType(after),
        before_blobs=MappingProxyType(before),
        changed_lines=MappingProxyType(changed_lines),
        deleted_lines=MappingProxyType(deleted_lines),
        deleted_paths=tuple(deleted_paths),
    )


@dataclass(frozen=True)
class ReviewerBinding:
    slot: str
    provider: str
    model: str
    model_family: str


def validate_reviewer_runtimes(
    runtimes: Iterable[Mapping[str, object] | ReviewerBinding],
) -> tuple[ReviewerBinding, ReviewerBinding]:
    """Resolve exactly two required, explicitly diverse reviewer lanes."""

    values = list(runtimes)
    if len(values) != len(_REQUIRED_SLOTS):
        raise ReviewValidationError("review requires exactly two reviewer slots")

    by_slot: dict[str, ReviewerBinding] = {}
    for item in values:
        if isinstance(item, ReviewerBinding):
            binding = ReviewerBinding(
                slot=_require_text(item.slot, "slot", maximum=64),
                provider=_require_text(item.provider, "provider", maximum=128),
                model=_require_text(item.model, "model", maximum=256),
                model_family=_require_text(
                    item.model_family, "model_family", maximum=128
                ).casefold(),
            )
        elif isinstance(item, Mapping):
            allowed = {"slot", "provider", "model", "model_family"}
            if set(item) != allowed:
                raise ReviewValidationError("reviewer runtime has unknown or missing fields")
            binding = ReviewerBinding(
                slot=_require_text(item["slot"], "slot", maximum=64),
                provider=_require_text(item["provider"], "provider", maximum=128),
                model=_require_text(item["model"], "model", maximum=256),
                model_family=_require_text(
                    item["model_family"], "model_family", maximum=128
                ).casefold(),
            )
        else:
            raise ReviewValidationError("reviewer runtime must be an object")
        if binding.slot not in _REQUIRED_SLOTS or binding.slot in by_slot:
            raise ReviewValidationError("reviewer slots must match the required unique slots")
        by_slot[binding.slot] = binding

    if set(by_slot) != set(_REQUIRED_SLOTS):
        raise ReviewValidationError("reviewer slots must match the required slots")
    ordered = tuple(by_slot[slot] for slot in _REQUIRED_SLOTS)
    if len({binding.model_family for binding in ordered}) != len(ordered):
        raise ReviewValidationError("reviewer model families must be distinct")
    return ordered  # type: ignore[return-value]


@dataclass(frozen=True)
class FindingReproduction:
    kind: str
    argv: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class ReviewLocator:
    kind: str
    path: str = ""
    start_line: int | None = None
    end_line: int | None = None
    locator_id: str = ""
    quoted_evidence: str = ""
    cited_bytes_sha256: str = ""


@dataclass(frozen=True)
class ReviewFinding:
    severity: str
    locator: ReviewLocator
    title: str
    trigger: str
    observed_failure: str
    blast_radius: str
    reproduction: FindingReproduction
    fingerprint: str

    @property
    def path(self) -> str:
        return self.locator.path

    @property
    def start_line(self) -> int | None:
        return self.locator.start_line

    @property
    def end_line(self) -> int | None:
        return self.locator.end_line

    @property
    def cited_bytes_sha256(self) -> str:
        return self.locator.cited_bytes_sha256


@dataclass(frozen=True)
class EvidenceContext:
    """Host-owned readers and declarations for one frozen review target."""

    read_frozen_file: Callable[[str], bytes] = field(repr=False, compare=False)
    diff_membership: Callable[[str, int, int], bool] = field(
        repr=False, compare=False
    )
    approved_lease_paths: tuple[str, ...]
    read_frozen_base_file: Callable[[str], bytes] | None = field(
        default=None, repr=False, compare=False
    )
    deleted_line_membership: Callable[[str, int, int], bool] | None = field(
        default=None, repr=False, compare=False
    )
    missing_artifacts: tuple[str, ...] = ()
    deleted_paths: tuple[str, ...] = ()
    unchanged_dependencies: tuple[str, ...] = ()
    contract_receipts: Mapping[str, bytes] = field(default_factory=dict)
    _issue_locators: Mapping[str, tuple[str, str]] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not callable(self.read_frozen_file) or not callable(self.diff_membership):
            raise ReviewValidationError("frozen evidence readers must be callable")
        if self.read_frozen_base_file is not None and not callable(
            self.read_frozen_base_file
        ):
            raise ReviewValidationError("frozen base evidence reader must be callable")
        if self.deleted_line_membership is not None and not callable(
            self.deleted_line_membership
        ):
            raise ReviewValidationError("deleted-line membership must be callable")
        leases = tuple(_require_lease_path(item) for item in self.approved_lease_paths)
        if len(set(leases)) != len(leases):
            raise ReviewValidationError("approved lease paths must be unique")
        missing = tuple(
            _require_relative_path(item, "missing artifact")
            for item in self.missing_artifacts
        )
        deleted = tuple(
            _require_relative_path(item, "deleted path") for item in self.deleted_paths
        )
        unchanged = tuple(
            _require_relative_path(item, "unchanged dependency")
            for item in self.unchanged_dependencies
        )
        if any(len(set(items)) != len(items) for items in (missing, deleted, unchanged)):
            raise ReviewValidationError("frozen evidence declarations must be unique")
        if not isinstance(self.contract_receipts, Mapping):
            raise ReviewValidationError("contract receipts must be a mapping")
        receipts: dict[str, bytes] = {}
        for key, value in self.contract_receipts.items():
            name = _require_text(key, "contract receipt ID", maximum=512)
            if not isinstance(value, bytes) or len(value) > _MAX_FROZEN_FILE_BYTES:
                raise ReviewValidationError("contract receipt evidence must be bounded bytes")
            receipts[name] = bytes(value)

        issues: dict[str, tuple[str, str]] = {}
        for kind, names in (
            ("missing_artifact", missing),
            ("deleted_path", deleted),
            ("contract_or_receipt", tuple(receipts)),
        ):
            for name in names:
                locator_id = _domain_digest(
                    _ISSUE_LOCATOR_DOMAIN,
                    _canonical_json({"identifier": name, "kind": kind}),
                )
                issues[locator_id] = (kind, name)

        object.__setattr__(self, "approved_lease_paths", leases)
        object.__setattr__(self, "missing_artifacts", missing)
        object.__setattr__(self, "deleted_paths", deleted)
        object.__setattr__(self, "unchanged_dependencies", unchanged)
        object.__setattr__(self, "contract_receipts", MappingProxyType(receipts))
        object.__setattr__(self, "_issue_locators", MappingProxyType(issues))

    def issue_locator(self, kind: str, identifier: str) -> str:
        kind = _require_text(kind, "locator kind", maximum=64)
        if kind not in {
            "missing_artifact",
            "deleted_path",
            "contract_or_receipt",
        }:
            raise ReviewValidationError("locator kind cannot use a host issue ID")
        name = (
            _require_relative_path(identifier)
            if kind != "contract_or_receipt"
            else _require_text(identifier, "contract receipt ID", maximum=512)
        )
        locator_id = _domain_digest(
            _ISSUE_LOCATOR_DOMAIN,
            _canonical_json({"identifier": name, "kind": kind}),
        )
        if self._issue_locators.get(locator_id) != (kind, name):
            raise ReviewValidationError("host evidence locator is not declared")
        return locator_id

    def resolve_issue_locator(self, locator_id: object, kind: str) -> str:
        locator_id = _require_digest(locator_id, "locator_id")
        resolved = self._issue_locators.get(locator_id)
        if resolved is None or resolved[0] != kind:
            raise ReviewValidationError("host evidence locator is stale or unknown")
        return resolved[1]

    def repair_authorized_path(self, path: str) -> bool:
        normalized = _require_relative_path(path)
        for lease in self.approved_lease_paths:
            if lease.endswith("/"):
                if normalized.startswith(lease):
                    return True
            elif normalized == lease:
                return True
        return False

    def frozen_file_bytes(self, path: str) -> bytes:
        normalized = _require_relative_path(path)
        try:
            value = self.read_frozen_file(normalized)
        except Exception as exc:
            raise ReviewValidationError(
                "frozen review file is unavailable"
            ) from exc
        if not isinstance(value, bytes) or len(value) > _MAX_FROZEN_FILE_BYTES:
            raise ReviewValidationError("frozen review file must be bounded bytes")
        return value

    def frozen_base_file_bytes(self, path: str) -> bytes:
        normalized = _require_relative_path(path)
        if self.read_frozen_base_file is None:
            raise ReviewValidationError("frozen base review file is unavailable")
        try:
            value = self.read_frozen_base_file(normalized)
        except Exception as exc:
            raise ReviewValidationError(
                "frozen base review file is unavailable"
            ) from exc
        if not isinstance(value, bytes) or len(value) > _MAX_FROZEN_FILE_BYTES:
            raise ReviewValidationError(
                "frozen base review file must be bounded bytes"
            )
        return value

    def range_is_changed(self, path: str, start_line: int, end_line: int) -> bool:
        try:
            value = self.diff_membership(path, start_line, end_line)
        except Exception as exc:
            raise ReviewValidationError("exact diff membership is unavailable") from exc
        if type(value) is not bool:
            raise ReviewValidationError("exact diff membership must be boolean")
        return value

    def range_is_deleted(self, path: str, start_line: int, end_line: int) -> bool:
        if self.deleted_line_membership is None:
            raise ReviewValidationError("deleted-line membership is unavailable")
        try:
            value = self.deleted_line_membership(path, start_line, end_line)
        except Exception as exc:
            raise ReviewValidationError(
                "deleted-line membership is unavailable"
            ) from exc
        if type(value) is not bool:
            raise ReviewValidationError("deleted-line membership must be boolean")
        return value


@dataclass(frozen=True)
class ReviewVerdict:
    target_digest: str
    integration_oid: str
    findings: tuple[ReviewFinding, ...]
    blocking_findings: tuple[ReviewFinding, ...]
    passed: bool


def _line_evidence(
    *,
    evidence: EvidenceContext,
    path: str,
    start_line: object,
    end_line: object,
    quoted_evidence: object,
    base: bool = False,
) -> tuple[int, int, str, str]:
    start = _require_positive_int(start_line, "start_line")
    end = _require_positive_int(end_line, "end_line")
    if end < start or end - start > 10_000:
        raise ReviewValidationError("review line range is invalid")
    if not isinstance(quoted_evidence, str) or "\x00" in quoted_evidence:
        raise ReviewValidationError("quoted evidence must be text")
    try:
        quoted_bytes = quoted_evidence.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ReviewValidationError("quoted evidence must be UTF-8") from exc
    if not quoted_bytes or len(quoted_bytes) > _MAX_FROZEN_FILE_BYTES:
        raise ReviewValidationError("quoted evidence must be bounded text")
    file_bytes = (
        evidence.frozen_base_file_bytes(path)
        if base
        else evidence.frozen_file_bytes(path)
    )
    lines = file_bytes.splitlines(keepends=True)
    if start > len(lines) or end > len(lines):
        raise ReviewValidationError("review line range is outside the frozen file")
    cited = b"".join(lines[start - 1 : end])
    if cited != quoted_bytes:
        raise ReviewValidationError("quoted evidence bytes are stale")
    return start, end, quoted_evidence, hashlib.sha256(cited).hexdigest()


def _parse_locator(value: object, evidence: EvidenceContext) -> ReviewLocator:
    if not isinstance(value, dict):
        raise ReviewValidationError("review locator must be an object")
    kind = value.get("kind")
    if not isinstance(kind, str) or kind not in _SUPPORTED_LOCATOR_KINDS:
        raise ReviewValidationError("review locator kind is unknown")

    if kind in {"changed_lines", "unchanged_dependency", "deleted_lines"}:
        if kind == "deleted_lines":
            required = {
                "kind",
                "path",
                "before_start_line",
                "before_end_line",
                "quoted_evidence",
            }
            if set(value) != required:
                raise ReviewValidationError(
                    "deleted-line locator has unknown or missing fields"
                )
            path = _require_relative_path(value["path"])
            start, end, quote, digest = _line_evidence(
                evidence=evidence,
                path=path,
                start_line=value["before_start_line"],
                end_line=value["before_end_line"],
                quoted_evidence=value["quoted_evidence"],
                base=True,
            )
            if not evidence.range_is_deleted(path, start, end):
                raise ReviewValidationError(
                    "review range is not in the exact deleted diff"
                )
            return ReviewLocator(
                kind=kind,
                path=path,
                start_line=start,
                end_line=end,
                quoted_evidence=quote,
                cited_bytes_sha256=digest,
            )
        required = {"kind", "path", "start_line", "end_line", "quoted_evidence"}
        if set(value) != required:
            raise ReviewValidationError("line locator has unknown or missing fields")
        path = _require_relative_path(value["path"])
        start, end, quote, digest = _line_evidence(
            evidence=evidence,
            path=path,
            start_line=value["start_line"],
            end_line=value["end_line"],
            quoted_evidence=value["quoted_evidence"],
        )
        if kind == "changed_lines":
            if not evidence.range_is_changed(path, start, end):
                raise ReviewValidationError("review range is not in the exact changed diff")
            if not evidence.repair_authorized_path(path):
                raise ReviewRequiresAuthority(
                    "changed review path is outside approved repair leases"
                )
        else:
            if path not in evidence.unchanged_dependencies:
                raise ReviewValidationError("unchanged dependency is not host-declared")
            if evidence.range_is_changed(path, start, end):
                raise ReviewValidationError("unchanged dependency is in the changed diff")
        return ReviewLocator(
            kind=kind,
            path=path,
            start_line=start,
            end_line=end,
            quoted_evidence=quote,
            cited_bytes_sha256=digest,
        )

    if kind in {"missing_artifact", "deleted_path"}:
        if set(value) != {"kind", "locator_id"}:
            raise ReviewValidationError("absence locator has unknown or missing fields")
        locator_id = _require_digest(value["locator_id"], "locator_id")
        path = evidence.resolve_issue_locator(locator_id, kind)
        cited = _canonical_json({"kind": kind, "path": path}).encode("utf-8")
        return ReviewLocator(
            kind=kind,
            path=path,
            locator_id=locator_id,
            cited_bytes_sha256=hashlib.sha256(cited).hexdigest(),
        )

    if set(value) != {"kind", "locator_id", "quoted_evidence"}:
        raise ReviewValidationError("receipt locator has unknown or missing fields")
    locator_id = _require_digest(value["locator_id"], "locator_id")
    receipt_id = evidence.resolve_issue_locator(locator_id, kind)
    quote = value["quoted_evidence"]
    if not isinstance(quote, str) or "\x00" in quote:
        raise ReviewValidationError("quoted receipt evidence must be text")
    try:
        quoted_bytes = quote.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ReviewValidationError("quoted receipt evidence must be UTF-8") from exc
    receipt_bytes = evidence.contract_receipts[receipt_id]
    if quoted_bytes != receipt_bytes:
        raise ReviewValidationError("quoted receipt evidence bytes are stale")
    return ReviewLocator(
        kind=kind,
        path=receipt_id,
        locator_id=locator_id,
        quoted_evidence=quote,
        cited_bytes_sha256=hashlib.sha256(receipt_bytes).hexdigest(),
    )


def _parse_reproduction(value: object) -> FindingReproduction:
    if not isinstance(value, dict):
        raise ReviewValidationError("finding reproduction must be an object")
    kind = value.get("kind")
    if kind == "command":
        if set(value) != {"kind", "argv"}:
            raise ReviewValidationError("command reproduction has unknown or missing fields")
        argv = value["argv"]
        if not isinstance(argv, list) or not 1 <= len(argv) <= 64:
            raise ReviewValidationError("command reproduction argv must be non-empty")
        normalized = tuple(
            _require_text(item, "command argument", maximum=4096) for item in argv
        )
        return FindingReproduction(kind="command", argv=normalized)
    if kind == "not_applicable":
        if set(value) != {"kind", "reason"}:
            raise ReviewValidationError(
                "not-applicable reproduction has unknown or missing fields"
            )
        return FindingReproduction(
            kind="not_applicable",
            reason=_require_text(value["reason"], "not-applicable reason"),
        )
    raise ReviewValidationError("finding reproduction kind is unknown")


def _parse_finding(
    value: object,
    *,
    target: ReviewTarget,
    evidence: EvidenceContext,
) -> ReviewFinding:
    if not isinstance(value, dict):
        raise ReviewValidationError("each review finding must be an object")
    required = {
        "severity",
        "locator",
        "title",
        "trigger",
        "observed_failure",
        "blast_radius",
        "reproduction",
    }
    if set(value) != required:
        raise ReviewValidationError("review finding has unknown or missing fields")
    severity = _require_text(value["severity"], "severity", maximum=16).casefold()
    if severity not in _SEVERITY_ORDER:
        raise ReviewValidationError("review finding has an unknown severity")
    locator = _parse_locator(value["locator"], evidence)
    title = _require_text(value["title"], "finding title", maximum=512)
    trigger = _require_text(value["trigger"], "finding trigger")
    observed_failure = _require_text(
        value["observed_failure"], "observed failure"
    )
    blast_radius = _require_text(value["blast_radius"], "blast radius")
    reproduction = _parse_reproduction(value["reproduction"])
    fingerprint_payload = {
        "blast_radius": blast_radius,
        "locator": {
            "cited_bytes_sha256": locator.cited_bytes_sha256,
            "end_line": locator.end_line,
            "kind": locator.kind,
            "locator_id": locator.locator_id,
            "path": locator.path,
            "start_line": locator.start_line,
        },
        "observed_failure": observed_failure,
        "reproduction": {
            "argv": list(reproduction.argv),
            "kind": reproduction.kind,
            "reason": reproduction.reason,
        },
        "severity": severity,
        "target_digest": target.target_digest,
        "title": title,
        "trigger": trigger,
    }
    return ReviewFinding(
        severity=severity,
        locator=locator,
        title=title,
        trigger=trigger,
        observed_failure=observed_failure,
        blast_radius=blast_radius,
        reproduction=reproduction,
        fingerprint=_domain_digest(
            _FINDING_DOMAIN, _canonical_json(fingerprint_payload)
        ),
    )


def parse_review_verdict(
    raw: str,
    *,
    target: ReviewTarget,
    evidence: EvidenceContext,
) -> ReviewVerdict:
    """Parse strict reviewer JSON and derive pass from its validated findings."""

    if not isinstance(target, ReviewTarget) or not isinstance(evidence, EvidenceContext):
        raise ReviewValidationError("review target or evidence context is invalid")
    value = _strict_json_object(raw, "reviewer output")
    required = {"schema", "target_digest", "integration_oid", "findings"}
    if set(value) != required:
        raise ReviewValidationError("reviewer output has unknown or missing fields")
    if value["schema"] != "hermes.bestplan.review-verdict.v1":
        raise ReviewValidationError("reviewer output uses an unknown schema")
    if value["target_digest"] != target.target_digest:
        raise ReviewValidationError("reviewer output targets a stale review target")
    if value["integration_oid"] != target.integration_oid:
        raise ReviewValidationError("reviewer output targets a stale integration")
    if not isinstance(value["findings"], list) or len(value["findings"]) > _MAX_FINDINGS:
        raise ReviewValidationError("review findings must be a bounded list")
    findings = tuple(
        _parse_finding(item, target=target, evidence=evidence)
        for item in value["findings"]
    )
    fingerprints = [item.fingerprint for item in findings]
    if len(set(fingerprints)) != len(fingerprints):
        raise ReviewValidationError("review findings contain a duplicate fingerprint")
    blocking = tuple(
        sorted(
            (item for item in findings if item.severity in {"critical", "high"}),
            key=lambda item: _SEVERITY_ORDER[item.severity],
        )
    )
    passed = not blocking
    return ReviewVerdict(
        target_digest=target.target_digest,
        integration_oid=target.integration_oid,
        findings=findings,
        blocking_findings=blocking,
        passed=passed,
    )


@dataclass(frozen=True)
class ReviewerReceipt:
    slot: str
    provider: str
    model: str
    model_family: str
    output_digest: str
    verdict: ReviewVerdict


@dataclass(frozen=True)
class ReviewGenerationReceipt:
    target_digest: str
    integration_oid: str
    reviewer_receipts: tuple[ReviewerReceipt, ...]
    blocking_findings: tuple[ReviewFinding, ...]
    passed: bool
    receipt_digest: str


def _review_request(packet: str) -> dict[str, object]:
    return {
        "messages": [
            {
                "role": "system",
                "content": (
                    "Review the exact immutable integration target. Return only "
                    "hermes.bestplan.review-verdict.v1 JSON. Do not use tools."
                ),
            },
            {
                "role": "user",
                "content": packet,
            },
        ],
        "tools": [],
    }


def run_review_generation(
    target: ReviewTarget,
    runtimes: Sequence[ReviewerBinding | Mapping[str, object]],
    *,
    artifact: ReviewArtifact,
    evidence: EvidenceContext,
    reviewer_call: Callable[[ReviewerBinding, dict[str, object]], str],
    receipt_callback: Callable[[ReviewerReceipt, str], object] | None = None,
) -> ReviewGenerationReceipt:
    """Run both required reviewers against byte-identical no-tool requests."""

    bindings = validate_reviewer_runtimes(runtimes)
    if not isinstance(artifact, ReviewArtifact):
        raise ReviewValidationError("review generation requires an immutable artifact")
    packet = build_review_packet(target, artifact=artifact)
    receipts: list[ReviewerReceipt] = []
    findings: list[ReviewFinding] = []
    for binding in bindings:
        request = _review_request(packet)
        raw = reviewer_call(binding, request)
        if not isinstance(raw, str):
            raise ReviewValidationError("reviewer output must be text")
        verdict = parse_review_verdict(raw, target=target, evidence=evidence)
        output_digest = hashlib.sha256(_OUTPUT_DOMAIN + raw.encode("utf-8")).hexdigest()
        reviewer_receipt = ReviewerReceipt(
                slot=binding.slot,
                provider=binding.provider,
                model=binding.model,
                model_family=binding.model_family,
                output_digest=output_digest,
                verdict=verdict,
            )
        receipts.append(reviewer_receipt)
        if receipt_callback is not None:
            receipt_callback(reviewer_receipt, raw)
        findings.extend(verdict.blocking_findings)
    blocking = tuple(sorted(findings, key=lambda item: _SEVERITY_ORDER[item.severity]))
    receipt_payload = {
        "integration_oid": target.integration_oid,
        "reviewers": [
            {
                "model": item.model,
                "model_family": item.model_family,
                "output_digest": item.output_digest,
                "provider": item.provider,
                "slot": item.slot,
            }
            for item in receipts
        ],
        "target_digest": target.target_digest,
    }
    return ReviewGenerationReceipt(
        target_digest=target.target_digest,
        integration_oid=target.integration_oid,
        reviewer_receipts=tuple(receipts),
        blocking_findings=blocking,
        passed=not blocking,
        receipt_digest=_domain_digest(_RECEIPT_DOMAIN, _canonical_json(receipt_payload)),
    )


def _manual_secure_directory(path: Path) -> Path:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ReviewValidationError("manual review snapshot directory is unsafe")
        path.chmod(0o700)
    except OSError as exc:
        raise ReviewValidationError(
            "manual review snapshot directory is unavailable"
        ) from exc
    return path


def _manual_store_immutable(path: Path, content: bytes) -> None:
    directory = _manual_secure_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".manual-review-", dir=directory)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            try:
                info = path.lstat()
                existing = path.read_bytes()
            except OSError as exc:
                raise ReviewValidationError(
                    "manual review immutable snapshot is unavailable"
                ) from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ReviewValidationError(
                    "manual review immutable snapshot path is unsafe"
                )
            if not hmac.compare_digest(existing, content):
                raise ReviewStoreConflict(
                    "manual review snapshot digest conflicts with stored bytes"
                )
        try:
            path.chmod(0o600)
            directory_descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError as exc:
            raise ReviewValidationError(
                "manual review immutable snapshot could not be secured"
            ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _manual_snapshot_root(owner: _ManualOwner) -> Path:
    return _manual_secure_directory(owner.state_db_path.parent / "review-snapshots")


def _manual_repository_id(workspace: Path) -> str:
    raw = _manual_git(workspace, "rev-parse", "--git-common-dir").strip()
    try:
        common = Path(raw.decode("utf-8", "strict"))
        if not common.is_absolute():
            common = workspace / common
        identity = str(common.resolve(strict=True))
    except (UnicodeError, OSError, RuntimeError) as exc:
        raise ReviewValidationError(
            "manual review repository identity is unavailable"
        ) from exc
    return _domain_digest(
        _MANUAL_REPOSITORY_DOMAIN,
        _canonical_json({"common_dir": identity, "workspace": str(workspace)}),
    )


def _persist_manual_capture(
    *,
    owner: _ManualOwner,
    job_id: str,
    generation: int,
    capture: _ManualCapture,
    check_receipt_digest: str,
) -> tuple[str, Path, Mapping[str, object]]:
    root = _manual_snapshot_root(owner)
    object_directory = _manual_secure_directory(root / "objects")
    manifest_directory = _manual_secure_directory(root / "manifests")
    diff_sha256 = hashlib.sha256(capture.diff_bytes).hexdigest()
    diff_path = object_directory / f"{diff_sha256}.diff"
    _manual_store_immutable(diff_path, capture.diff_bytes)
    manifest = {
        "adapter_version": _MANUAL_ADAPTER_VERSION,
        "base_oid": capture.base_oid,
        "base_tree_oid": capture.base_tree_oid,
        "changed_paths": list(capture.changed_paths),
        "check_receipt_digest": _require_digest(
            check_receipt_digest, "manual review check receipt"
        ),
        "diff_object": f"objects/{diff_path.name}",
        "diff_sha256": diff_sha256,
        "files": [
            {
                "after_sha256": (
                    hashlib.sha256(capture.after_blobs[path]).hexdigest()
                    if path in capture.after_blobs
                    else None
                ),
                "before_sha256": (
                    hashlib.sha256(capture.before_blobs[path]).hexdigest()
                    if path in capture.before_blobs
                    else None
                ),
                "path": path,
            }
            for path in capture.changed_paths
        ],
        "generation": generation,
        "job_id": job_id,
        "live_state_digest": capture.live_state_digest,
        "owner": {
            "profile": owner.profile,
            "session_id": owner.session_id,
            "workspace": str(owner.workspace),
        },
        "path_states": [dict(item) for item in capture.path_states],
        "repository_control_digest": capture.repository_control_digest,
        "schema": _MANUAL_SNAPSHOT_SCHEMA,
        "snapshot_tree_oid": capture.snapshot_tree_oid,
    }
    canonical = _canonical_json(manifest)
    snapshot_digest = _domain_digest(_MANUAL_SNAPSHOT_DOMAIN, canonical)
    manifest_path = manifest_directory / f"{snapshot_digest}.json"
    _manual_store_immutable(manifest_path, canonical.encode("utf-8"))
    adapter_state: Mapping[str, object] = MappingProxyType(
        {
            "changed_paths": list(capture.changed_paths),
            "initial_snapshot_digest": snapshot_digest,
            "schema": _MANUAL_SNAPSHOT_SCHEMA,
            "snapshot_root": str(root.resolve()),
        }
    )
    return snapshot_digest, manifest_path, adapter_state


def _restore_manual_capture(
    *,
    owner: _ManualOwner,
    job_id: str,
    generation: int,
    snapshot_digest: str,
) -> _ManualCapture:
    """Restore and validate one immutable manual snapshot without live recapture."""

    snapshot_digest = _require_digest(snapshot_digest, "snapshot_digest")
    root = _manual_snapshot_root(owner)
    manifest_path = root / "manifests" / f"{snapshot_digest}.json"
    try:
        manifest_info = manifest_path.lstat()
        canonical = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReviewStoreConflict(
            "manual recovery snapshot manifest is unavailable"
        ) from exc
    if stat.S_ISLNK(manifest_info.st_mode) or not stat.S_ISREG(
        manifest_info.st_mode
    ):
        raise ReviewStoreConflict("manual recovery snapshot path is unsafe")
    if _domain_digest(_MANUAL_SNAPSHOT_DOMAIN, canonical) != snapshot_digest:
        raise ReviewStoreConflict("manual recovery snapshot digest differs")
    manifest = _strict_json_object(canonical, "manual recovery snapshot")
    owner_payload = manifest.get("owner")
    if (
        manifest.get("schema") != _MANUAL_SNAPSHOT_SCHEMA
        or manifest.get("adapter_version") != _MANUAL_ADAPTER_VERSION
        or manifest.get("job_id") != job_id
        or manifest.get("generation") != generation
        or owner_payload
        != {
            "profile": owner.profile,
            "session_id": owner.session_id,
            "workspace": str(owner.workspace),
        }
    ):
        raise ReviewStoreConflict("manual recovery snapshot identity differs")
    raw_paths = manifest.get("changed_paths")
    raw_states = manifest.get("path_states")
    raw_files = manifest.get("files")
    if (
        not isinstance(raw_paths, list)
        or not isinstance(raw_states, list)
        or not isinstance(raw_files, list)
    ):
        raise ReviewStoreConflict("manual recovery snapshot paths are invalid")
    paths = tuple(_require_relative_path(item) for item in raw_paths)
    if (
        not paths
        or len(paths) != len(set(paths))
        or list(paths) != sorted(paths)
        or len(raw_states) != len(paths)
        or len(raw_files) != len(paths)
    ):
        raise ReviewStoreConflict("manual recovery snapshot paths differ")
    base_oid = _require_oid(manifest.get("base_oid"), "manual recovery base")
    base_tree_oid = _require_oid(
        manifest.get("base_tree_oid"), "manual recovery base tree"
    )
    snapshot_tree_oid = _require_oid(
        manifest.get("snapshot_tree_oid"), "manual recovery snapshot tree"
    )
    if (
        _manual_git(owner.workspace, "rev-parse", f"{base_oid}^{{tree}}")
        .strip()
        .decode("ascii")
        != base_tree_oid
        or _manual_git(
            owner.workspace,
            "rev-parse",
            f"{snapshot_tree_oid}^{{tree}}",
        )
        .strip()
        .decode("ascii")
        != snapshot_tree_oid
    ):
        raise ReviewStoreConflict("manual recovery snapshot Git identity differs")
    diff_object = manifest.get("diff_object")
    if (
        not isinstance(diff_object, str)
        or diff_object != f"objects/{manifest.get('diff_sha256')}.diff"
    ):
        raise ReviewStoreConflict("manual recovery diff locator is invalid")
    diff_path = root / diff_object
    try:
        diff_info = diff_path.lstat()
        diff_bytes = diff_path.read_bytes()
    except OSError as exc:
        raise ReviewStoreConflict("manual recovery diff is unavailable") from exc
    if (
        stat.S_ISLNK(diff_info.st_mode)
        or not stat.S_ISREG(diff_info.st_mode)
        or hashlib.sha256(diff_bytes).hexdigest() != manifest.get("diff_sha256")
    ):
        raise ReviewStoreConflict("manual recovery diff differs")
    fresh_diff = _manual_git(
        owner.workspace,
        "diff",
        "--binary",
        "--full-index",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
        base_oid,
        snapshot_tree_oid,
        "--",
        *paths,
    )
    if not hmac.compare_digest(fresh_diff, diff_bytes):
        raise ReviewStoreConflict("manual recovery Git diff differs")
    files_by_path = {
        str(item.get("path")): item
        for item in raw_files
        if isinstance(item, Mapping)
    }
    states_by_path = {
        str(item.get("path")): item
        for item in raw_states
        if isinstance(item, Mapping)
    }
    if set(files_by_path) != set(paths) or set(states_by_path) != set(paths):
        raise ReviewStoreConflict("manual recovery file evidence differs")
    after: dict[str, bytes] = {}
    before: dict[str, bytes] = {}
    changed_lines: dict[str, frozenset[int]] = {}
    deleted_lines: dict[str, frozenset[int]] = {}
    deleted_paths: list[str] = []
    normalized_states: list[Mapping[str, object]] = []
    for path in paths:
        file_payload = files_by_path[path]
        state_payload = dict(states_by_path[path])
        if state_payload.get("kind") == "file":
            after_bytes = _manual_blob_at(owner.workspace, snapshot_tree_oid, path)
            if (
                hashlib.sha256(after_bytes).hexdigest()
                != state_payload.get("sha256")
                or len(after_bytes) != state_payload.get("size")
                or hashlib.sha256(after_bytes).hexdigest()
                != file_payload.get("after_sha256")
            ):
                raise ReviewStoreConflict(
                    "manual recovery after-image differs"
                )
            after[path] = after_bytes
        elif state_payload != {"kind": "absent", "path": path}:
            raise ReviewStoreConflict("manual recovery path state is invalid")
        try:
            before_bytes = _manual_blob_at(owner.workspace, base_oid, path)
            before[path] = before_bytes
            if hashlib.sha256(before_bytes).hexdigest() != file_payload.get(
                "before_sha256"
            ):
                raise ReviewStoreConflict(
                    "manual recovery before-image differs"
                )
        except FileNotFoundError:
            before_bytes = b""
            if file_payload.get("before_sha256") is not None:
                raise ReviewStoreConflict(
                    "manual recovery before-image differs"
                )
        if path not in after and path in before:
            deleted_paths.append(path)
        changed, deleted = _manual_line_sets(before_bytes, after.get(path, b""))
        changed_lines[path] = changed
        deleted_lines[path] = deleted
        normalized_states.append(MappingProxyType(state_payload))
    live_state_digest = _domain_digest(
        b"hermes.manual-review-live-state.v1\0",
        _canonical_json([dict(item) for item in normalized_states]),
    )
    if live_state_digest != manifest.get("live_state_digest"):
        raise ReviewStoreConflict("manual recovery live-state digest differs")
    return _ManualCapture(
        base_oid=base_oid,
        base_tree_oid=base_tree_oid,
        snapshot_tree_oid=snapshot_tree_oid,
        diff_bytes=diff_bytes,
        changed_paths=paths,
        live_state_digest=live_state_digest,
        repository_control_digest=_require_digest(
            manifest.get("repository_control_digest"),
            "manual recovery repository control",
        ),
        path_states=tuple(normalized_states),
        after_blobs=MappingProxyType(after),
        before_blobs=MappingProxyType(before),
        changed_lines=MappingProxyType(changed_lines),
        deleted_lines=MappingProxyType(deleted_lines),
        deleted_paths=tuple(deleted_paths),
    )


def _manual_task(conversation_history: Sequence[Mapping[str, object]]) -> str:
    for message in reversed(tuple(conversation_history)):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip() and "\x00" not in content:
            return content
    return "Review the selected manual objective."


def _manual_active_review_hint(
    conversation_history: Sequence[Mapping[str, object]],
) -> str | None:
    for message in reversed(tuple(conversation_history)):
        metadata = message.get("display_metadata")
        if not isinstance(metadata, Mapping):
            continue
        review = metadata.get("bestplan_review")
        if not isinstance(review, Mapping) or review.get("schema") != (
            "hermes.bestplan.active-review.v1"
        ):
            continue
        job_id = review.get("job_id")
        if isinstance(job_id, str) and job_id and "\x00" not in job_id:
            return job_id
    return None


def _bestplan_attachment_evidence(
    *,
    workspace: Path,
    target: ReviewTarget,
    artifact: ReviewArtifact,
) -> EvidenceContext:
    if _manual_git(
        workspace, "rev-parse", "--verify", f"{target.integration_ref}^{{commit}}"
    ).strip().decode("ascii") != target.integration_oid:
        raise ReviewStoreConflict("active BestPlan integration ref changed")
    if _manual_git(
        workspace, "rev-parse", f"{target.integration_oid}^{{tree}}"
    ).strip().decode("ascii") != target.integration_tree_oid:
        raise ReviewStoreConflict("active BestPlan integration tree changed")
    diff_bytes = _manual_git(
        workspace,
        "diff",
        "--binary",
        "--full-index",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
        target.base_oid,
        target.integration_oid,
        "--",
    )
    artifact_payload = _strict_json_object(
        artifact.canonical_json, "active BestPlan artifact"
    )
    stored_diff = artifact_payload.get("git_diff")
    if not isinstance(stored_diff, dict):
        raise ReviewStoreConflict("active BestPlan artifact diff is invalid")
    try:
        expected_diff = base64.b64decode(
            stored_diff["content_base64"], validate=True
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ReviewStoreConflict("active BestPlan artifact diff is invalid") from exc
    if not hmac.compare_digest(diff_bytes, expected_diff):
        raise ReviewStoreConflict("active BestPlan artifact differs from Git evidence")
    names = _manual_git(
        workspace,
        "diff",
        "--name-only",
        "-z",
        "--no-renames",
        target.base_oid,
        target.integration_oid,
        "--",
    ).split(b"\0")
    try:
        changed_paths = tuple(
            sorted(
                _require_relative_path(name.decode("utf-8", "strict"))
                for name in names
                if name
            )
        )
    except UnicodeError as exc:
        raise ReviewStoreConflict("active BestPlan path is invalid") from exc
    before_blobs: dict[str, bytes] = {}
    after_blobs: dict[str, bytes] = {}
    changed_lines: dict[str, frozenset[int]] = {}
    deleted_lines: dict[str, frozenset[int]] = {}
    for path in changed_paths:
        try:
            before = _manual_blob_at(workspace, target.base_oid, path)
            before_blobs[path] = before
        except FileNotFoundError:
            before = b""
        try:
            after = _manual_blob_at(workspace, target.integration_oid, path)
            after_blobs[path] = after
        except FileNotFoundError:
            after = b""
        changed, deleted = _manual_line_sets(before, after)
        changed_lines[path] = changed
        deleted_lines[path] = deleted
    catalog = artifact_payload.get("issue_locator_catalog")
    if not isinstance(catalog, dict):
        raise ReviewStoreConflict("active BestPlan issue catalog is invalid")
    missing: list[str] = []
    deleted_paths: list[str] = []
    unchanged: list[str] = []
    contract_receipts: dict[str, bytes] = {}
    for raw in catalog.values():
        if not isinstance(raw, dict):
            raise ReviewStoreConflict("active BestPlan issue catalog is invalid")
        kind = raw.get("kind")
        identifier = raw.get("identifier")
        if not isinstance(identifier, str):
            raise ReviewStoreConflict("active BestPlan issue catalog is invalid")
        if kind == "missing_artifact":
            missing.append(identifier)
        elif kind == "deleted_path":
            deleted_paths.append(identifier)
        elif kind == "unchanged_dependency":
            unchanged.append(identifier)
            if identifier not in after_blobs:
                after_blobs[identifier] = _manual_blob_at(
                    workspace, target.integration_oid, identifier
                )
        elif kind == "contract_or_receipt":
            quote = raw.get("quoted_evidence")
            if not isinstance(quote, str):
                raise ReviewStoreConflict("active BestPlan receipt evidence is invalid")
            contract_receipts[identifier] = quote.encode("utf-8")

    def read_after(path: str) -> bytes:
        return after_blobs[path]

    def read_before(path: str) -> bytes:
        return before_blobs[path]

    def is_changed(path: str, start: int, end: int) -> bool:
        lines = changed_lines.get(path, frozenset())
        return bool(lines) and all(line in lines for line in range(start, end + 1))

    def is_deleted(path: str, start: int, end: int) -> bool:
        lines = deleted_lines.get(path, frozenset())
        return bool(lines) and all(line in lines for line in range(start, end + 1))

    return EvidenceContext(
        read_frozen_file=read_after,
        read_frozen_base_file=read_before,
        diff_membership=is_changed,
        deleted_line_membership=is_deleted,
        approved_lease_paths=changed_paths,
        missing_artifacts=tuple(sorted(missing)),
        deleted_paths=tuple(sorted(deleted_paths)),
        unchanged_dependencies=tuple(sorted(unchanged)),
        contract_receipts=contract_receipts,
    )


def _manual_active_bestplan_attachment(
    *,
    owner: _ManualOwner,
    conversation_history: Sequence[Mapping[str, object]],
    policy_digest: str,
) -> _ActiveBestplanAttachment | None:
    hinted_job_id = _manual_active_review_hint(conversation_history)
    if hinted_job_id is None:
        return None
    store = ReviewStore(owner.state_db_path)
    job = store.find_active_bestplan_job(
        owner_session_id=owner.session_id,
        owner_profile=owner.profile,
        workspace=owner.workspace,
        hinted_job_id=hinted_job_id,
    )
    if job is None:
        return None
    if job.policy_digest != policy_digest or job.current_generation is None:
        raise ReviewStoreConflict("active BestPlan reviewer policy differs")
    generation = store.get_generation(job.job_id, int(job.current_generation))
    if generation.artifact_json is None:
        raise ReviewStoreConflict("active BestPlan generation has no review artifact")
    target = attach_manual_target(
        active_bestplan_target=_restore_review_target(generation.target_json)
    )
    artifact = _restore_review_artifact(
        generation.artifact_json,
        target=target,
    )
    if (
        target.target_digest != job.target_digest
        or target.target_digest != generation.target_digest
        or target.generation != generation.generation
    ):
        raise ReviewStoreConflict("active BestPlan generation identity differs")
    return _ActiveBestplanAttachment(
        job=job,
        generation=generation,
        target=target,
        artifact=artifact,
        evidence=_bestplan_attachment_evidence(
            workspace=owner.workspace,
            target=target,
            artifact=artifact,
        ),
    )


def _manual_job_id(
    owner: _ManualOwner,
    capture: _ManualCapture | None,
    *,
    identity_digest: str = "",
) -> str:
    payload = {
        "base_oid": None if capture is None else capture.base_oid,
        "changed_paths": [] if capture is None else list(capture.changed_paths),
        "identity_digest": identity_digest,
        "live_state_digest": (
            None if capture is None else capture.live_state_digest
        ),
        "nonce_ns": time.time_ns() if capture is None else None,
        "session_id": owner.session_id,
        "snapshot_tree_oid": None if capture is None else capture.snapshot_tree_oid,
        "workspace": str(owner.workspace),
    }
    return "manual-review-" + _domain_digest(
        _MANUAL_JOB_DOMAIN, _canonical_json(payload)
    )[:48]


def _manual_runtime_routes(
    bindings: Sequence[ReviewerBinding],
) -> list[dict[str, str]]:
    return [
        {
            "model": binding.model,
            "provider": binding.provider,
            "route": binding.slot,
            "runtime_fingerprint": _domain_digest(
                b"hermes.manual-review-runtime.v1\0",
                _canonical_json(
                    {
                        "model": binding.model,
                        "model_family": binding.model_family,
                        "provider": binding.provider,
                        "slot": binding.slot,
                    }
                ),
            ),
        }
        for binding in bindings
    ]


def _manual_finding_payload(finding: ReviewFinding) -> dict[str, object]:
    locator: dict[str, object] = {"kind": finding.locator.kind}
    for name in ("path", "locator_id", "quoted_evidence", "cited_bytes_sha256"):
        value = getattr(finding.locator, name)
        if value:
            locator[name] = value
    if finding.locator.start_line is not None:
        locator["start_line"] = finding.locator.start_line
    if finding.locator.end_line is not None:
        locator["end_line"] = finding.locator.end_line
    reproduction: dict[str, object] = {"kind": finding.reproduction.kind}
    if finding.reproduction.argv:
        reproduction["argv"] = list(finding.reproduction.argv)
    if finding.reproduction.reason:
        reproduction["reason"] = finding.reproduction.reason
    return {
        "blast_radius": finding.blast_radius,
        "fingerprint": finding.fingerprint,
        "locator": locator,
        "observed_failure": finding.observed_failure,
        "reproduction": reproduction,
        "severity": finding.severity,
        "title": finding.title,
        "trigger": finding.trigger,
    }


def _manual_reviewer_receipt_json(receipt: ReviewerReceipt) -> str:
    return _canonical_json(
        {
            "findings": [
                _manual_finding_payload(item) for item in receipt.verdict.findings
            ],
            "integration_oid": receipt.verdict.integration_oid,
            "model": receipt.model,
            "model_family": receipt.model_family,
            "output_digest": receipt.output_digest,
            "passed": receipt.verdict.passed,
            "provider": receipt.provider,
            "schema": "hermes.bestplan.stored-reviewer-receipt.v1",
            "slot": receipt.slot,
            "target_digest": receipt.verdict.target_digest,
        }
    )


def _manual_restore_reviewer_receipt(
    stored: "StoredReviewerReceipt",
    *,
    bundle: _ManualReviewBundle,
) -> ReviewerReceipt:
    """Rebuild one validated reviewer receipt without replaying its model call."""

    value = _strict_json_object(stored.receipt_json, "stored reviewer receipt")
    required = {
        "findings",
        "integration_oid",
        "model",
        "model_family",
        "output_digest",
        "passed",
        "provider",
        "schema",
        "slot",
        "target_digest",
    }
    if set(value) != required or value["schema"] != (
        "hermes.bestplan.stored-reviewer-receipt.v1"
    ):
        raise ReviewStoreConflict("stored manual reviewer receipt is invalid")
    raw_findings = value["findings"]
    if not isinstance(raw_findings, list):
        raise ReviewStoreConflict("stored manual findings are invalid")
    findings: list[dict[str, object]] = []
    for raw in raw_findings:
        if not isinstance(raw, dict):
            raise ReviewStoreConflict("stored manual finding is invalid")
        finding = dict(raw)
        finding.pop("fingerprint", None)
        locator = finding.get("locator")
        if not isinstance(locator, dict):
            raise ReviewStoreConflict("stored manual locator is invalid")
        locator = dict(locator)
        locator.pop("cited_bytes_sha256", None)
        if locator.get("kind") == "deleted_lines":
            locator["before_start_line"] = locator.pop("start_line", None)
            locator["before_end_line"] = locator.pop("end_line", None)
        finding["locator"] = locator
        findings.append(finding)
    verdict_raw = _canonical_json(
        {
            "findings": findings,
            "integration_oid": value["integration_oid"],
            "schema": "hermes.bestplan.review-verdict.v1",
            "target_digest": value["target_digest"],
        }
    )
    verdict = parse_review_verdict(
        verdict_raw,
        target=bundle.target,
        evidence=bundle.evidence,
    )
    binding_values = {
        "model": _require_text(value["model"], "stored reviewer model"),
        "model_family": _require_text(
            value["model_family"], "stored reviewer model family"
        ).casefold(),
        "provider": _require_text(value["provider"], "stored reviewer provider"),
        "slot": _require_text(value["slot"], "stored reviewer slot"),
    }
    if (
        binding_values["slot"] != stored.slot
        or value["target_digest"] != stored.target_digest
        or value["integration_oid"] != stored.integration_oid
        or value["output_digest"] != stored.output_digest
        or bool(value["passed"]) != stored.passed
        or verdict.passed != stored.passed
    ):
        raise ReviewStoreConflict("stored manual reviewer receipt conflicts")
    return ReviewerReceipt(
        output_digest=stored.output_digest,
        verdict=verdict,
        **binding_values,
    )


def _manual_generation_receipt(
    target: ReviewTarget,
    receipts: Sequence[ReviewerReceipt],
) -> ReviewGenerationReceipt:
    by_slot = {item.slot: item for item in receipts}
    if set(by_slot) != set(_REQUIRED_SLOTS):
        raise ReviewStoreConflict("manual review requires both reviewer receipts")
    ordered = tuple(by_slot[slot] for slot in _REQUIRED_SLOTS)
    blockers = tuple(
        sorted(
            (
                finding
                for receipt in ordered
                for finding in receipt.verdict.blocking_findings
            ),
            key=lambda item: _SEVERITY_ORDER[item.severity],
        )
    )
    receipt_payload = {
        "integration_oid": target.integration_oid,
        "reviewers": [
            {
                "model": item.model,
                "model_family": item.model_family,
                "output_digest": item.output_digest,
                "provider": item.provider,
                "slot": item.slot,
            }
            for item in ordered
        ],
        "target_digest": target.target_digest,
    }
    return ReviewGenerationReceipt(
        target_digest=target.target_digest,
        integration_oid=target.integration_oid,
        reviewer_receipts=ordered,
        blocking_findings=blockers,
        passed=not blockers,
        receipt_digest=_domain_digest(
            _RECEIPT_DOMAIN, _canonical_json(receipt_payload)
        ),
    )


def _manual_call_reviewer(
    binding: ReviewerBinding,
    *,
    bundle: _ManualReviewBundle,
    reviewer_call: Callable[[ReviewerBinding, dict[str, object]], str],
) -> ReviewerReceipt:
    packet = build_review_packet(bundle.target, artifact=bundle.artifact)
    raw = reviewer_call(binding, _review_request(packet))
    if not isinstance(raw, str):
        raise ReviewValidationError("reviewer output must be text")
    verdict = parse_review_verdict(
        raw,
        target=bundle.target,
        evidence=bundle.evidence,
    )
    return ReviewerReceipt(
        slot=binding.slot,
        provider=binding.provider,
        model=binding.model,
        model_family=binding.model_family,
        output_digest=hashlib.sha256(
            _OUTPUT_DOMAIN + raw.encode("utf-8")
        ).hexdigest(),
        verdict=verdict,
    )


def _manual_interrupt_requested(agent: object) -> bool:
    if bool(getattr(agent, "_interrupt_requested", False)):
        return True
    hard = getattr(agent, "_hard_interrupt_requested", None)
    is_set = getattr(hard, "is_set", None)
    return bool(callable(is_set) and is_set())


def _manual_operation_lock_descriptor(
    store: "ReviewStore",
    job_id: str,
) -> int:
    """Lock one manual job across owner-lease turnover and host effects."""

    lock_root = _manual_secure_directory(
        Path(store.path).parent / "review-operation-locks"
    )
    leaf = hashlib.sha256(
        b"hermes.manual-review-operation-lock.v1\0" + job_id.encode("utf-8")
    ).hexdigest() + ".lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    root_descriptor = os.open(
        lock_root,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        descriptor = os.open(leaf, flags, 0o600, dir_fd=root_descriptor)
    finally:
        os.close(root_descriptor)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != os.getuid()
            or opened.st_mode & 0o077
        ):
            raise ReviewStoreConflict("manual operation lock identity is invalid")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _manual_controlled_call(
    *,
    agent: object,
    store: "ReviewStore",
    job_id: str,
    owner_id: str,
    fencing_token: int,
    cancel_operation_id: str,
    cancel_event: threading.Event,
    adoption_lock: threading.Lock,
    call: Callable[[], object],
) -> object:
    """Run one host call with lease renewal and durable cancellation control."""

    operation_lock = _manual_operation_lock_descriptor(store, job_id)
    try:
        return _manual_controlled_call_under_lock(
            agent=agent,
            store=store,
            job_id=job_id,
            owner_id=owner_id,
            fencing_token=fencing_token,
            cancel_operation_id=cancel_operation_id,
            cancel_event=cancel_event,
            adoption_lock=adoption_lock,
            call=call,
        )
    finally:
        os.close(operation_lock)


def _manual_controlled_call_under_lock(
    *,
    agent: object,
    store: "ReviewStore",
    job_id: str,
    owner_id: str,
    fencing_token: int,
    cancel_operation_id: str,
    cancel_event: threading.Event,
    adoption_lock: threading.Lock,
    call: Callable[[], object],
) -> object:
    """Execute one host effect while the immutable job operation lock is held."""

    # Validate and extend the exact fence before the child can make a model or
    # filesystem effect. A paused, expired worker must never race a reclaimer.
    try:
        leased_job = store.renew_lease(
            job_id=job_id,
            owner_id=owner_id,
            fencing_token=fencing_token,
            now_ns=time.time_ns(),
            lease_duration_ns=_MANUAL_REVIEW_LEASE_NS,
        )
    except Exception as exc:
        cancel_event.set()
        try:
            current = store.get_job(job_id)
            if (
                current.cancel_requested
                and current.owner_id == owner_id
                and current.fencing_token == fencing_token
            ):
                store.finalize_cancel(
                    job_id=job_id,
                    owner_id=owner_id,
                    fencing_token=fencing_token,
                    operation_id=cancel_operation_id + ":no-child-started",
                )
        except Exception:
            pass
        raise _ManualInvocationCancelled(
            "manual review owner lease is no longer valid"
        ) from exc

    values: list[object] = []
    failures: list[BaseException] = []
    completed = threading.Event()

    def invoke() -> None:
        try:
            values.append(call())
        except BaseException as exc:
            failures.append(exc)
        finally:
            completed.set()

    worker = threading.Thread(
        target=invoke,
        name=f"hermes-manual-review-{fencing_token}",
        daemon=True,
    )
    worker.start()
    next_renewal = time.monotonic() + _MANUAL_REVIEW_HEARTBEAT_SECONDS

    def persist_cancel() -> None:
        current = store.get_job(job_id)
        if current.cancel_requested:
            cancel_event.set()
            return
        store.request_manual_cancel_intent(
            job_id=job_id,
            expected_target_digest=leased_job.target_digest,
            operation_id=cancel_operation_id,
            signal_children=cancel_event.set,
        )

    def signal_local_cancel() -> None:
        cancel_event.set()

    def persist_cancel_until_durable() -> None:
        """Do not release a cancelled child until its stop intent is durable."""

        attempts = 0
        while True:
            try:
                persist_cancel()
                return
            except Exception:
                delay = min(
                    _MANUAL_CANCEL_REQUEST_RETRY_MAX_SECONDS,
                    _MANUAL_CANCEL_REQUEST_RETRY_BASE_SECONDS
                    * (2 ** min(attempts, 7)),
                )
                attempts += 1
                time.sleep(delay)

    def finalize_cancel_when_extinct(*, wait: bool) -> bool:
        if wait and not completed.wait(_MANUAL_CANCEL_EXTINCTION_SECONDS):
            return False
        if not completed.is_set():
            return False
        with adoption_lock:
            current = store.get_job(job_id)
            if current.state == "cancelled":
                return True
            if not current.cancel_requested:
                return False
            if (
                current.owner_id != owner_id
                or current.fencing_token != fencing_token
            ):
                # A newer owner claimed the same immutable job before the
                # operator stop committed. The durable cancel bit now fences
                # that owner; only its child-extinction proof may finalize it.
                return False
            store.finalize_cancel(
                job_id=job_id,
                owner_id=owner_id,
                fencing_token=fencing_token,
                operation_id=cancel_operation_id + ":extinct",
            )
        return True

    def supervise_extinction() -> None:
        completed.wait()
        attempts = 0
        while True:
            try:
                if finalize_cancel_when_extinct(wait=False):
                    return
                current = store.get_job(job_id)
                if current.state == "cancelled" or not current.cancel_requested:
                    return
                if (
                    current.owner_id != owner_id
                    or current.fencing_token != fencing_token
                ):
                    return
            except Exception:
                pass
            delay = min(
                _MANUAL_CANCEL_FINALIZE_RETRY_MAX_SECONDS,
                _MANUAL_CANCEL_FINALIZE_RETRY_BASE_SECONDS
                * (2 ** min(attempts, 7)),
            )
            attempts += 1
            time.sleep(delay)

    def cancel_and_raise(*, persist: bool) -> None:
        if persist:
            persist_cancel_until_durable()
        else:
            signal_local_cancel()
        try:
            finalized = finalize_cancel_when_extinct(wait=True)
        except Exception:
            finalized = False
        if not finalized:
            threading.Thread(
                target=supervise_extinction,
                name=f"hermes-manual-cancel-{fencing_token}",
                daemon=True,
            ).start()
        raise _ManualInvocationCancelled("manual review was cancelled")

    while True:
        now = time.monotonic()
        wait_seconds = max(
            0.001,
            min(
                _MANUAL_REVIEW_CONTROL_POLL_SECONDS,
                max(0.0, next_renewal - now),
            ),
        )
        if completed.wait(wait_seconds):
            break
        if _manual_interrupt_requested(agent):
            cancel_and_raise(persist=True)
        now = time.monotonic()
        if now < next_renewal:
            continue
        try:
            store.renew_lease(
                job_id=job_id,
                owner_id=owner_id,
                fencing_token=fencing_token,
                now_ns=time.time_ns(),
                lease_duration_ns=_MANUAL_REVIEW_LEASE_NS,
            )
        except Exception:
            cancel_and_raise(persist=True)
        next_renewal = now + _MANUAL_REVIEW_HEARTBEAT_SECONDS

    if _manual_interrupt_requested(agent):
        cancel_and_raise(persist=True)
    if cancel_event.is_set() or store.get_job(job_id).cancel_requested:
        cancel_and_raise(persist=False)
    if failures:
        raise failures[0]
    if len(values) != 1:
        raise ReviewStoreConflict("manual host call returned no result")
    return values[0]


def _manual_check_failure_finding(
    *,
    target: ReviewTarget,
    path: str,
    reason: str,
    attempt: int,
) -> ReviewFinding:
    normalized_reason = reason.strip() or "the deterministic objective check failed"
    locator = ReviewLocator(kind="contract_or_receipt", path=path)
    reproduction = FindingReproduction(
        kind="not_applicable",
        reason=normalized_reason,
    )
    payload = {
        "attempt": attempt,
        "path": path,
        "reason": normalized_reason,
        "target_digest": target.target_digest,
    }
    return ReviewFinding(
        severity="high",
        locator=locator,
        title="Deterministic objective check failed",
        trigger=normalized_reason,
        observed_failure=normalized_reason,
        blast_radius="The selected objective cannot pass its exact checks.",
        reproduction=reproduction,
        fingerprint=_domain_digest(
            _FINDING_DOMAIN, _canonical_json(payload)
        ),
    )


def _manual_build_bundle(
    *,
    owner: _ManualOwner,
    job_id: str,
    generation: int,
    capture: _ManualCapture,
    task: str,
    acceptance: Sequence[str],
    acceptance_digest: str,
    policy_digest: str,
    check_receipt_digest: str,
    dispositions: Sequence[Mapping[str, object]],
) -> _ManualReviewBundle:
    snapshot_digest, manifest_path, adapter_state = _persist_manual_capture(
        owner=owner,
        job_id=job_id,
        generation=generation,
        capture=capture,
        check_receipt_digest=check_receipt_digest,
    )
    target = ReviewTarget.manual_snapshot(
        job_id=job_id,
        generation=generation,
        repository_id=_manual_repository_id(owner.workspace),
        base_oid=capture.base_oid,
        snapshot_tree_oid=capture.snapshot_tree_oid,
        snapshot_digest=snapshot_digest,
        diff_sha256=hashlib.sha256(capture.diff_bytes).hexdigest(),
        acceptance_digest=acceptance_digest,
        policy_digest=policy_digest,
        check_receipt_digest=check_receipt_digest,
    )

    def read_frozen_file(path: str) -> bytes:
        try:
            return capture.after_blobs[path]
        except KeyError as exc:
            raise ReviewValidationError("frozen manual review file is unavailable") from exc

    def read_frozen_base_file(path: str) -> bytes:
        try:
            return capture.before_blobs[path]
        except KeyError as exc:
            raise ReviewValidationError(
                "frozen manual review base file is unavailable"
            ) from exc

    def diff_membership(path: str, start_line: int, end_line: int) -> bool:
        lines = capture.changed_lines.get(path, frozenset())
        return bool(lines) and all(
            line in lines for line in range(start_line, end_line + 1)
        )

    def deleted_membership(path: str, start_line: int, end_line: int) -> bool:
        lines = capture.deleted_lines.get(path, frozenset())
        return bool(lines) and all(
            line in lines for line in range(start_line, end_line + 1)
        )

    contract_receipts: dict[str, bytes] = {}
    if check_receipt_digest != _MANUAL_ZERO_DIGEST:
        contract_receipts[f"manual-check-generation-{generation}"] = _canonical_json(
            {
                "generation": generation,
                "receipt_digest": check_receipt_digest,
                "schema": "hermes.manual-review-check-receipt.v1",
            }
        ).encode("utf-8")
    evidence = EvidenceContext(
        read_frozen_file=read_frozen_file,
        read_frozen_base_file=read_frozen_base_file,
        diff_membership=diff_membership,
        deleted_line_membership=deleted_membership,
        approved_lease_paths=capture.changed_paths,
        deleted_paths=capture.deleted_paths,
        contract_receipts=contract_receipts,
    )
    issue_catalog: dict[str, dict[str, str]] = {}
    for deleted_path in capture.deleted_paths:
        locator_id = evidence.issue_locator("deleted_path", deleted_path)
        issue_catalog[locator_id] = {
            "identifier": deleted_path,
            "kind": "deleted_path",
        }
    for receipt_id, receipt_bytes in contract_receipts.items():
        locator_id = evidence.issue_locator("contract_or_receipt", receipt_id)
        issue_catalog[locator_id] = {
            "identifier": receipt_id,
            "kind": "contract_or_receipt",
            "quoted_evidence": receipt_bytes.decode("utf-8"),
        }
    artifact = ReviewArtifact.build(
        target=target,
        diff_bytes=capture.diff_bytes,
        task=task,
        acceptance=acceptance,
        rules=(
            "CRITICAL and HIGH findings block this exact generation.",
            "Repairs may change only the selected objective paths.",
            "Return only the required strict JSON verdict and do not use tools.",
        ),
        issue_locator_catalog=issue_catalog,
        dispositions=dispositions,
    )
    return _ManualReviewBundle(
        target=target,
        artifact=artifact,
        evidence=evidence,
        capture=capture,
        manifest_path=manifest_path,
        adapter_state=adapter_state,
    )


def _manual_target_is_current(owner: _ManualOwner, capture: _ManualCapture) -> bool:
    try:
        head = _manual_git(owner.workspace, "rev-parse", "HEAD^{commit}").strip().decode(
            "ascii"
        )
        return (
            hmac.compare_digest(head, capture.base_oid)
            and hmac.compare_digest(
                _manual_repository_control_digest(owner.workspace),
                capture.repository_control_digest,
            )
            and hmac.compare_digest(
                _manual_live_state_digest(owner.workspace, capture.changed_paths),
                capture.live_state_digest,
            )
        )
    except (ReviewValidationError, UnicodeError):
        return False


def _manual_reconcile_repair_capture(
    *,
    owner: _ManualOwner,
    prior_capture: _ManualCapture,
    repaired_capture: _ManualCapture,
    ambient_digest: str,
    cancel_event: threading.Event,
    adoption_lock: threading.Lock,
) -> None:
    """Adopt or roll forward one exact prepared manual repair effect."""

    if not hmac.compare_digest(
        _manual_ambient_digest(owner.workspace, prior_capture.changed_paths),
        _require_digest(ambient_digest, "manual checkpoint ambient digest"),
    ):
        raise ReviewStoreConflict("manual recovery ambient target changed")
    prior_by_path = {
        str(item["path"]): dict(item) for item in prior_capture.path_states
    }
    repaired_by_path = {
        str(item["path"]): dict(item) for item in repaired_capture.path_states
    }
    needs_apply: list[str] = []
    for path in prior_capture.changed_paths:
        live = _manual_path_state(owner.workspace, path)
        if live == repaired_by_path[path]:
            continue
        if live == prior_by_path[path]:
            needs_apply.append(path)
            continue
        raise ReviewStoreConflict("manual recovery live target changed")
    if needs_apply:
        from agent.manual_review_runtime import _apply_repaired_paths

        root = _manual_snapshot_root(owner)
        with tempfile.TemporaryDirectory(
            prefix="manual-recovery-apply-", dir=root
        ) as raw_materialized:
            materialized = Path(raw_materialized)
            for path in repaired_capture.changed_paths:
                state = repaired_by_path[path]
                if state.get("kind") != "file":
                    continue
                target = materialized / path
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                target.write_bytes(repaired_capture.after_blobs[path])
                target.chmod(0o755 if state.get("mode") == "100755" else 0o644)
            _apply_repaired_paths(
                workspace=owner.workspace,
                materialized_root=materialized,
                prefix="",
                root_paths=tuple(needs_apply),
                cancel_event=cancel_event,
                adoption_lock=adoption_lock,
            )
    if not hmac.compare_digest(
        _manual_live_state_digest(
            owner.workspace, repaired_capture.changed_paths
        ),
        repaired_capture.live_state_digest,
    ):
        raise ReviewStoreConflict("manual recovery repair adoption differs")


def _manual_transition(
    store: "ReviewStore",
    *,
    job_id: str,
    generation: int,
    owner_id: str,
    fencing_token: int,
    state: str,
    event_kind: str,
    target_digest: str,
    payload: Mapping[str, object],
    operation_id: str | None = None,
    release_lease: bool = False,
) -> "ReviewJob":
    if state not in {
        "blocked_no_target",
        "blocked_requires_authority",
        "checking",
        "repairing",
        "reviewing",
        "waiting",
    }:
        raise ReviewValidationError("manual review state is unsupported")

    def write(connection: sqlite3.Connection) -> ReviewJob:
        row = store._require_lease(
            connection,
            job_id=job_id,
            owner_id=owner_id,
            fencing_token=fencing_token,
        )
        current = row["current_generation"]
        if current is not None and int(current) != generation:
            raise ReviewStoreConflict("manual review generation is no longer current")
        if release_lease:
            changed = connection.execute(
                """
                UPDATE review_jobs
                SET state=?, owner_id=NULL, lease_expires_at_ns=NULL
                WHERE job_id=? AND owner_id=? AND fencing_token=?
                  AND cancel_requested=0
                """,
                (state, job_id, owner_id, fencing_token),
            ).rowcount
        else:
            changed = connection.execute(
                """
                UPDATE review_jobs SET state=?
                WHERE job_id=? AND owner_id=? AND fencing_token=?
                  AND cancel_requested=0
                """,
                (state, job_id, owner_id, fencing_token),
            ).rowcount
        if changed != 1:
            raise ReviewLeaseConflict("manual review state lost its owner lease")
        generation_row = connection.execute(
            "SELECT 1 FROM review_generations WHERE job_id=? AND generation=?",
            (job_id, generation),
        ).fetchone()
        if generation_row is not None:
            connection.execute(
                "UPDATE review_generations SET state=? WHERE job_id=? AND generation=?",
                (state, job_id, generation),
            )
        store._append_event_conn(
            connection,
            job_id=job_id,
            generation=generation,
            owner_id=owner_id,
            fencing_token=fencing_token,
            operation_id=(
                operation_id or f"manual:{generation}:{event_kind}"
            ),
            kind=event_kind,
            target_digest=target_digest,
            payload=dict(payload),
        )
        return store._job_from_row(store._job_row(connection, job_id))

    return store._write(write)  # type: ignore[return-value]


def _manual_release_existing_state(
    store: "ReviewStore",
    *,
    job_id: str,
    generation: int,
    owner_id: str,
    fencing_token: int,
    event_kind: str,
    target_digest: str,
    payload: Mapping[str, object],
    operation_id: str,
) -> "ReviewJob":
    """Release a manual attachment without rewriting its automatic state."""

    def write(connection: sqlite3.Connection) -> ReviewJob:
        row = store._require_lease(
            connection,
            job_id=job_id,
            owner_id=owner_id,
            fencing_token=fencing_token,
        )
        if row["current_generation"] != generation:
            raise ReviewStoreConflict("manual attachment generation is stale")
        if connection.execute(
            """
            UPDATE review_jobs SET owner_id=NULL, lease_expires_at_ns=NULL
            WHERE job_id=? AND owner_id=? AND fencing_token=?
              AND cancel_requested=0
            """,
            (job_id, owner_id, fencing_token),
        ).rowcount != 1:
            raise ReviewLeaseConflict("manual attachment release lost ownership")
        store._append_event_conn(
            connection,
            job_id=job_id,
            generation=generation,
            owner_id=owner_id,
            fencing_token=fencing_token,
            operation_id=operation_id,
            kind=event_kind,
            target_digest=target_digest,
            payload=dict(payload),
        )
        return store._job_from_row(store._job_row(connection, job_id))

    return store._write(write)  # type: ignore[return-value]


def _manual_result(
    conversation_history: Sequence[Mapping[str, object]],
    *,
    response: str,
    completed: bool,
    api_calls: int,
    job_id: str,
    state: str,
    interrupted: bool = False,
) -> dict[str, object]:
    messages = [dict(item) for item in conversation_history]
    messages.append({"role": "assistant", "content": response})
    return {
        "api_calls": api_calls,
        "completed": completed,
        "failed": not completed,
        "final_response": response,
        "interrupted": interrupted,
        "messages": messages,
        "partial": False,
        "review_job_id": job_id,
        "review_state": state,
    }


def _manual_cancel_response(state: str, *, attached: bool = False) -> str:
    if state == "cancelled":
        if attached:
            return "The active BestPlan review was cancelled."
        return "Manual review was cancelled."
    if attached:
        return (
            "Cancellation was requested. The active BestPlan review is waiting "
            "for its child operation to stop."
        )
    return (
        "Cancellation was requested. Manual review is waiting for its child "
        "operation to stop."
    )


def build_manual_review_resume_request(
    *,
    state_db_path: str | Path,
    job_id: str,
) -> dict[str, str]:
    """Build one secret-free request for the shared recovery worker."""

    try:
        canonical_state = Path(state_db_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError) as exc:
        raise ReviewValidationError("manual recovery state is unavailable") from exc
    job = ReviewStore(canonical_state).get_job(job_id)
    if (
        job.adapter_version != _MANUAL_ADAPTER_VERSION
        or job.source_kind != "manual_snapshot"
        or not job.owner_session_id
        or not job.owner_profile
        or not job.workspace
    ):
        raise ReviewStoreConflict("manual recovery job identity is invalid")
    return {
        "adapter_version": _MANUAL_ADAPTER_VERSION,
        "job_id": job.job_id,
        "kind": "manual_review_resume",
        "profile": job.owner_profile,
        "schema": "hermes.manual-review-resume.v1",
        "session_id": job.owner_session_id,
        "state_db_path": str(canonical_state),
        "workspace": job.workspace,
    }


def resume_manual_review_job(
    agent: object,
    request: Mapping[str, object],
) -> dict[str, object]:
    """Resume one exact durable manual job with freshly resolved authorities."""

    if not isinstance(request, Mapping) or set(request) != {
        "adapter_version",
        "job_id",
        "kind",
        "profile",
        "schema",
        "session_id",
        "state_db_path",
        "workspace",
    }:
        raise ReviewValidationError("manual recovery request is invalid")
    expected = build_manual_review_resume_request(
        state_db_path=request.get("state_db_path"),
        job_id=_require_text(request.get("job_id"), "manual recovery job"),
    )
    if dict(request) != expected:
        raise ReviewStoreConflict("manual recovery request identity differs")
    prior = getattr(agent, "_manual_review_resume_job_id", None)
    setattr(agent, "_manual_review_resume_job_id", expected["job_id"])
    try:
        return run_manual_review_request(
            agent,
            scope="",
            conversation_history=(),
        )
    finally:
        if prior is None:
            try:
                delattr(agent, "_manual_review_resume_job_id")
            except AttributeError:
                pass
        else:
            setattr(agent, "_manual_review_resume_job_id", prior)


def _clear_review_enqueue_retry(key: tuple[str, str, str]) -> None:
    with _REVIEW_ENQUEUE_RETRY_LOCK:
        _REVIEW_ENQUEUE_RETRY_SCHEDULED.discard(key)
        _REVIEW_ENQUEUE_RETRY_ATTEMPTS.pop(key, None)


def _schedule_review_enqueue_retry(
    *,
    kind: str,
    state_db_path: Path,
    job_id: str,
) -> None:
    key = (kind, str(state_db_path), job_id)
    with _REVIEW_ENQUEUE_RETRY_LOCK:
        if key in _REVIEW_ENQUEUE_RETRY_SCHEDULED:
            return
        attempts = _REVIEW_ENQUEUE_RETRY_ATTEMPTS.get(key, 0)
        _REVIEW_ENQUEUE_RETRY_ATTEMPTS[key] = attempts + 1
        _REVIEW_ENQUEUE_RETRY_SCHEDULED.add(key)
    delay = min(
        _REVIEW_ENQUEUE_RETRY_MAX_SECONDS,
        _REVIEW_ENQUEUE_RETRY_BASE_SECONDS * (2 ** min(attempts, 7)),
    )

    def retry() -> None:
        with _REVIEW_ENQUEUE_RETRY_LOCK:
            if key not in _REVIEW_ENQUEUE_RETRY_SCHEDULED:
                return
            _REVIEW_ENQUEUE_RETRY_SCHEDULED.discard(key)
        try:
            if kind == "manual":
                _enqueue_manual_review_resume(
                    state_db_path=state_db_path,
                    job_id=job_id,
                )
            else:
                _enqueue_attached_bestplan_resume(
                    state_db_path=state_db_path,
                    job_id=job_id,
                )
        except Exception:
            _schedule_review_enqueue_retry(
                kind=kind,
                state_db_path=state_db_path,
                job_id=job_id,
            )

    timer = threading.Timer(delay, retry)
    timer.name = f"review-enqueue-retry-{kind}"
    timer.daemon = True
    timer.start()


def _enqueue_manual_review_resume(
    *,
    state_db_path: Path,
    job_id: str,
) -> bool:
    request = build_manual_review_resume_request(
        state_db_path=state_db_path,
        job_id=job_id,
    )
    canonical_state = Path(str(request["state_db_path"]))
    key = ("manual", str(canonical_state), job_id)
    try:
        from tools.async_delegation import enqueue_manual_review_recovery

        accepted = enqueue_manual_review_recovery(request) is True
    except (ImportError, AttributeError):
        accepted = False
    if accepted:
        _clear_review_enqueue_retry(key)
    else:
        _schedule_review_enqueue_retry(
            kind="manual",
            state_db_path=canonical_state,
            job_id=job_id,
        )
    return accepted


def _enqueue_attached_bestplan_resume(
    *,
    state_db_path: Path,
    job_id: str,
) -> bool:
    try:
        canonical_state = state_db_path.resolve(strict=True)
        from tools.async_delegation import enqueue_bestplan_review_job

        accepted = enqueue_bestplan_review_job(
            state_db_path=str(canonical_state),
            job_id=job_id,
        ) is True
    except (ImportError, AttributeError, OSError):
        accepted = False
        canonical_state = state_db_path.resolve(strict=False)
    key = ("attached", str(canonical_state), job_id)
    if accepted:
        _clear_review_enqueue_retry(key)
    else:
        _schedule_review_enqueue_retry(
            kind="attached",
            state_db_path=canonical_state,
            job_id=job_id,
        )
    return accepted


def _manual_result_paths(
    owner: _ManualOwner,
    value: object,
) -> tuple[str, ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or not value
        or len(value) > _MANUAL_SNAPSHOT_MAX_FILES
    ):
        raise ReviewRequiresAuthority("manual repair paths are invalid")
    paths: list[str] = []
    for raw in value:
        path, directory_scope = _manual_relative_path(owner.workspace, raw)
        if directory_scope:
            raise ReviewRequiresAuthority("manual repair paths must identify files")
        paths.append(path)
    if len(set(paths)) != len(paths):
        raise ReviewRequiresAuthority("manual repair paths are ambiguous")
    return tuple(sorted(paths))


def _manual_no_target(
    *,
    owner: _ManualOwner,
    conversation_history: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    base_oid = _require_oid(
        _manual_git(owner.workspace, "rev-parse", "HEAD^{commit}").strip().decode(
            "ascii"
        ),
        "manual review base OID",
    )
    tree_oid = _require_oid(
        _manual_git(owner.workspace, "rev-parse", "HEAD^{tree}").strip().decode(
            "ascii"
        ),
        "manual review base tree OID",
    )
    job_id = _manual_job_id(owner, None)
    policy_digest = _domain_digest(
        b"hermes.manual-review-no-target-policy.v1\0",
        _canonical_json({"state": "blocked_no_target"}),
    )
    acceptance_digest = _domain_digest(
        _MANUAL_ACCEPTANCE_DOMAIN,
        _canonical_json({"changed_paths": [], "task": "manual review"}),
    )
    target = ReviewTarget.manual_snapshot(
        job_id=job_id,
        generation=0,
        repository_id=_manual_repository_id(owner.workspace),
        base_oid=base_oid,
        snapshot_tree_oid=tree_oid,
        snapshot_digest=_domain_digest(
            _MANUAL_SNAPSHOT_DOMAIN,
            _canonical_json(
                {
                    "job_id": job_id,
                    "owner_session_id": owner.session_id,
                    "state": "blocked_no_target",
                }
            ),
        ),
        diff_sha256=hashlib.sha256(b"").hexdigest(),
        acceptance_digest=acceptance_digest,
        policy_digest=policy_digest,
    )
    store = ReviewStore(owner.state_db_path)
    store.create_job(
        job_id=job_id,
        source_kind=target.source_kind,
        source_id=target.plan_id,
        target_digest=target.target_digest,
        policy_digest=target.policy_digest,
        integration_oid=target.integration_oid,
        check_receipt_digest=target.check_receipt_digest,
        adapter_version=_MANUAL_ADAPTER_VERSION,
        owner_session_id=owner.session_id,
        owner_profile=owner.profile,
        workspace=str(owner.workspace),
        adapter_state={
            "changed_paths": [],
            "no_target": True,
            "schema": _MANUAL_SNAPSHOT_SCHEMA,
            "snapshot_root": str(owner.state_db_path.parent / "review-snapshots"),
        },
        runtime_routes=[],
    )
    lease_owner = "manual-" + secrets.token_hex(16)
    claim = store.claim_job(
        job_id=job_id,
        owner_id=lease_owner,
        now_ns=time.time_ns(),
        lease_duration_ns=_MANUAL_REVIEW_LEASE_NS,
        expected_fencing_token=0,
    )
    _manual_transition(
        store,
        job_id=job_id,
        generation=0,
        owner_id=lease_owner,
        fencing_token=claim.fencing_token,
        state="blocked_no_target",
        event_kind="blocked_no_target",
        target_digest=target.target_digest,
        payload={"reason": "no objective artifact is available"},
    )
    return _manual_result(
        conversation_history,
        response=(
            "No objective artifact is available for manual review. "
            "Make or select an objective change, then run /review again."
        ),
        completed=False,
        api_calls=0,
        job_id=job_id,
        state="blocked_no_target",
    )


def _restore_bestplan_reviewer_receipt(
    stored: "StoredReviewerReceipt",
    *,
    attachment: _ActiveBestplanAttachment,
) -> ReviewerReceipt:
    value = _strict_json_object(stored.receipt_json, "stored reviewer receipt")
    raw_output = value.get("raw_output")
    if (
        value.get("schema") != "hermes.bestplan.stored-reviewer-receipt.v1"
        or value.get("slot") != stored.slot
        or value.get("target_digest") != attachment.target.target_digest
        or value.get("integration_oid") != attachment.target.integration_oid
        or not isinstance(raw_output, str)
    ):
        raise ReviewStoreConflict("stored BestPlan reviewer receipt is invalid")
    verdict = parse_review_verdict(
        raw_output,
        target=attachment.target,
        evidence=attachment.evidence,
    )
    output_digest = hashlib.sha256(
        _OUTPUT_DOMAIN + raw_output.encode("utf-8")
    ).hexdigest()
    if output_digest != stored.output_digest or verdict.passed != stored.passed:
        raise ReviewStoreConflict("stored BestPlan reviewer receipt conflicts")
    return ReviewerReceipt(
        slot=_require_text(value.get("slot"), "stored reviewer slot"),
        provider=_require_text(value.get("provider"), "stored reviewer provider"),
        model=_require_text(value.get("model"), "stored reviewer model"),
        model_family=_require_text(
            value.get("model_family"), "stored reviewer model family"
        ).casefold(),
        output_digest=output_digest,
        verdict=verdict,
    )


def _run_attached_bestplan_review(
    agent: object,
    *,
    owner: _ManualOwner,
    attachment: _ActiveBestplanAttachment,
    runtime: object,
    bindings: tuple[ReviewerBinding, ReviewerBinding],
    reviewer_call: Callable[[ReviewerBinding, dict[str, object]], str],
    conversation_history: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    store = ReviewStore(owner.state_db_path)
    job_id = attachment.job.job_id
    lease_owner = "manual-" + secrets.token_hex(16)
    try:
        claim = store.claim_job(
            job_id=job_id,
            owner_id=lease_owner,
            now_ns=time.time_ns(),
            lease_duration_ns=_MANUAL_REVIEW_LEASE_NS,
            expected_fencing_token=attachment.job.fencing_token,
        )
    except ReviewLeaseConflict:
        active = store.get_job(job_id)
        return _manual_result(
            conversation_history,
            response="The active BestPlan review already has a live owner.",
            completed=active.state == "passed",
            api_calls=0,
            job_id=job_id,
            state=active.state,
        )
    resume = store.resume_job(
        job_id=job_id,
        owner_id=lease_owner,
        fencing_token=claim.fencing_token,
    )
    if resume.next_action == "handoff_pass":
        _manual_release_existing_state(
            store,
            job_id=job_id,
            generation=attachment.target.generation,
            owner_id=lease_owner,
            fencing_token=claim.fencing_token,
            event_kind="manual_attachment_pass_released",
            target_digest=attachment.target.target_digest,
            payload={"automatic_resume": True},
            operation_id=(
                f"manual-attach:{attachment.target.generation}:pass-release:"
                f"{claim.fencing_token}"
            ),
        )
        _enqueue_attached_bestplan_resume(
            state_db_path=owner.state_db_path,
            job_id=job_id,
        )
        return _manual_result(
            conversation_history,
            response="The active BestPlan review already passed.",
            completed=True,
            api_calls=0,
            job_id=job_id,
            state="passed",
        )
    if resume.next_action != "review_missing_slots":
        _manual_release_existing_state(
            store,
            job_id=job_id,
            generation=attachment.target.generation,
            owner_id=lease_owner,
            fencing_token=claim.fencing_token,
            event_kind="manual_attachment_deferred",
            target_digest=attachment.target.target_digest,
            payload={"next_action": resume.next_action},
            operation_id=(
                f"manual-attach:{attachment.target.generation}:deferred:"
                f"{claim.fencing_token}"
            ),
        )
        _enqueue_attached_bestplan_resume(
            state_db_path=owner.state_db_path,
            job_id=job_id,
        )
        return _manual_result(
            conversation_history,
            response="The active BestPlan review is continuing its automatic loop.",
            completed=False,
            api_calls=0,
            job_id=job_id,
            state=claim.state,
        )
    routes = json.loads(claim.runtime_routes_json)
    if not isinstance(routes, list):
        raise ReviewStoreConflict("active BestPlan runtime routes are invalid")
    fingerprints = {
        str(item.get("route")): str(item.get("runtime_fingerprint"))
        for item in routes
        if isinstance(item, dict)
    }
    route_identities = {
        str(item.get("route")): (
            str(item.get("provider")),
            str(item.get("model")),
        )
        for item in routes
        if isinstance(item, dict)
    }
    if any(
        route_identities.get(binding.slot) != (binding.provider, binding.model)
        for binding in bindings
    ):
        _manual_transition(
            store,
            job_id=job_id,
            generation=attachment.target.generation,
            owner_id=lease_owner,
            fencing_token=claim.fencing_token,
            state="waiting",
            event_kind="reviewer_identity_drift",
            target_digest=attachment.target.target_digest,
            payload={"reason": "active BestPlan reviewer route changed"},
            operation_id=(
                f"manual-attach:{attachment.target.generation}:identity-drift:"
                f"{claim.fencing_token}"
            ),
            release_lease=True,
        )
        return _manual_result(
            conversation_history,
            response="The active BestPlan reviewer identity changed. Review is waiting.",
            completed=False,
            api_calls=0,
            job_id=job_id,
            state="waiting",
        )
    restored = {
        item.slot: _restore_bestplan_reviewer_receipt(
            item,
            attachment=attachment,
        )
        for item in resume.adopted_reviewer_receipts
    }
    cancel_event = threading.Event()
    adoption_lock = threading.Lock()
    api_calls = 0
    receipts = dict(restored)

    def wait_for_reviewer_recovery(
        binding: ReviewerBinding,
        exc: BaseException,
    ) -> dict[str, object]:
        _manual_release_existing_state(
            store,
            job_id=job_id,
            generation=attachment.target.generation,
            owner_id=lease_owner,
            fencing_token=claim.fencing_token,
            event_kind="reviewer_failure",
            target_digest=attachment.target.target_digest,
            payload={
                "error_type": type(exc).__name__,
                "slot": binding.slot,
            },
            operation_id=(
                f"manual-attach:{attachment.target.generation}:"
                f"reviewer-failure:{binding.slot}:{claim.fencing_token}"
            ),
        )
        _enqueue_attached_bestplan_resume(
            state_db_path=owner.state_db_path,
            job_id=job_id,
        )
        return _manual_result(
            conversation_history,
            response=(
                "The active BestPlan reviewer is waiting for automatic "
                "recovery."
            ),
            completed=False,
            api_calls=api_calls,
            job_id=job_id,
            state="reviewing",
        )

    for binding in bindings:
        if binding.slot in receipts:
            continue
        inline_reviewer_failures = 0
        while True:
            api_calls += 1
            raw_values: list[str] = []

            def call_reviewer() -> ReviewerReceipt:
                packet = build_review_packet(
                    attachment.target,
                    artifact=attachment.artifact,
                )
                raw = reviewer_call(binding, _review_request(packet))
                if not isinstance(raw, str):
                    raise ReviewValidationError("reviewer output must be text")
                raw_values.append(raw)
                verdict = parse_review_verdict(
                    raw,
                    target=attachment.target,
                    evidence=attachment.evidence,
                )
                return ReviewerReceipt(
                    slot=binding.slot,
                    provider=binding.provider,
                    model=binding.model,
                    model_family=binding.model_family,
                    output_digest=hashlib.sha256(
                        _OUTPUT_DOMAIN + raw.encode("utf-8")
                    ).hexdigest(),
                    verdict=verdict,
                )

            try:
                receipt = _manual_controlled_call(
                    agent=agent,
                    store=store,
                    job_id=job_id,
                    owner_id=lease_owner,
                    fencing_token=claim.fencing_token,
                    cancel_operation_id=(
                        f"manual-attach:{attachment.target.generation}:cancel:"
                        f"{claim.fencing_token}"
                    ),
                    cancel_event=cancel_event,
                    adoption_lock=adoption_lock,
                    call=call_reviewer,
                )
            except _ManualInvocationCancelled:
                current = store.get_job(job_id)
                return _manual_result(
                    conversation_history,
                    response=_manual_cancel_response(
                        current.state,
                        attached=True,
                    ),
                    completed=False,
                    api_calls=api_calls,
                    job_id=job_id,
                    state=current.state,
                    interrupted=True,
                )
            except Exception as exc:
                inline_reviewer_failures += 1
                if (
                    isinstance(exc, ConnectionError)
                    or inline_reviewer_failures > 1
                ):
                    return wait_for_reviewer_recovery(binding, exc)
                refresh = getattr(runtime, "refresh_reviewers", None)
                if callable(refresh):
                    try:
                        refresh()
                        raw_refreshed = tuple(
                            getattr(runtime, "reviewer_bindings", ())
                        )
                        refreshed = validate_reviewer_runtimes(
                            raw_refreshed
                        )
                        from agent.bestplan_review import bestplan_review_policy_digest

                        identity_drift = bestplan_review_policy_digest(refreshed) != (
                            attachment.target.policy_digest
                        ) or any(
                            route_identities.get(item.slot)
                            != (item.provider, item.model)
                            for item in refreshed
                        )
                    except ReviewValidationError:
                        identity_drift = True
                    except Exception as refresh_exc:
                        return wait_for_reviewer_recovery(
                            binding, refresh_exc,
                        )
                    if identity_drift:
                        _manual_transition(
                            store,
                            job_id=job_id,
                            generation=attachment.target.generation,
                            owner_id=lease_owner,
                            fencing_token=claim.fencing_token,
                            state="waiting",
                            event_kind="reviewer_identity_drift",
                            target_digest=attachment.target.target_digest,
                            payload={"reason": "reviewer identity changed on retry"},
                            operation_id=(
                                f"manual-attach:{attachment.target.generation}:"
                                f"identity-drift:{claim.fencing_token}"
                            ),
                            release_lease=True,
                        )
                        return _manual_result(
                            conversation_history,
                            response=(
                                "The active BestPlan reviewer identity changed. "
                                "Review is waiting."
                            ),
                            completed=False,
                            api_calls=api_calls,
                            job_id=job_id,
                            state="waiting",
                        )
                    refreshed_by_slot = {
                        item.slot: item for item in refreshed
                    }
                    if binding.slot not in refreshed_by_slot:
                        return wait_for_reviewer_recovery(binding, exc)
                    raw_refreshed_by_slot = {
                        item.slot: item
                        for item in raw_refreshed
                        if isinstance(item, ReviewerBinding)
                    }
                    refreshed_binding = refreshed_by_slot[binding.slot]
                    raw_binding = raw_refreshed_by_slot.get(binding.slot)
                    binding = (
                        raw_binding
                        if raw_binding == refreshed_binding
                        else refreshed_binding
                    )
                continue
            assert isinstance(receipt, ReviewerReceipt)
            raw_output = raw_values[0]
            payload = _canonical_json(
                {
                    "findings": [
                        _manual_finding_payload(item)
                        for item in receipt.verdict.findings
                    ],
                    "integration_oid": attachment.target.integration_oid,
                    "model": receipt.model,
                    "model_family": receipt.model_family,
                    "output_digest": receipt.output_digest,
                    "provider": receipt.provider,
                    "raw_output": raw_output,
                    "runtime_fingerprint": fingerprints.get(receipt.slot, ""),
                    "schema": "hermes.bestplan.stored-reviewer-receipt.v1",
                    "slot": receipt.slot,
                    "target_digest": attachment.target.target_digest,
                }
            )
            store.record_reviewer_receipt(
                job_id=job_id,
                generation=attachment.target.generation,
                slot=receipt.slot,
                target_digest=attachment.target.target_digest,
                integration_oid=attachment.target.integration_oid,
                output_digest=receipt.output_digest,
                verdict_digest=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                passed=receipt.verdict.passed,
                receipt_json=payload,
                owner_id=lease_owner,
                fencing_token=claim.fencing_token,
                operation_id=(
                    f"manual-attach:{attachment.target.generation}:reviewer:"
                    f"{receipt.slot}"
                ),
            )
            receipts[receipt.slot] = receipt
            break
    generation_receipt = _manual_generation_receipt(
        attachment.target,
        tuple(receipts.values()),
    )
    if generation_receipt.passed:
        store.record_generation_pass(
            job_id=job_id,
            generation=attachment.target.generation,
            target_digest=attachment.target.target_digest,
            integration_oid=attachment.target.integration_oid,
            check_receipt_digest=attachment.target.check_receipt_digest,
            review_receipt_digest=generation_receipt.receipt_digest,
            owner_id=lease_owner,
            fencing_token=claim.fencing_token,
            operation_id=(
                f"manual-attach:{attachment.target.generation}:review-pass"
            ),
        )
        _manual_release_existing_state(
            store,
            job_id=job_id,
            generation=attachment.target.generation,
            owner_id=lease_owner,
            fencing_token=claim.fencing_token,
            event_kind="manual_attachment_pass_released",
            target_digest=attachment.target.target_digest,
            payload={"automatic_resume": True},
            operation_id=(
                f"manual-attach:{attachment.target.generation}:pass-release:"
                f"{claim.fencing_token}"
            ),
        )
        _enqueue_attached_bestplan_resume(
            state_db_path=owner.state_db_path,
            job_id=job_id,
        )
        return _manual_result(
            conversation_history,
            response="The active BestPlan review passed both reviewer lanes.",
            completed=True,
            api_calls=api_calls,
            job_id=job_id,
            state="passed",
        )
    store.record_generation_blocked(
        job_id=job_id,
        generation=attachment.target.generation,
        target_digest=attachment.target.target_digest,
        integration_oid=attachment.target.integration_oid,
        check_receipt_digest=attachment.target.check_receipt_digest,
        review_receipt_digest=generation_receipt.receipt_digest,
        blocking_findings_json=_canonical_json(
            [
                _manual_finding_payload(item)
                for item in generation_receipt.blocking_findings
            ]
        ),
        owner_id=lease_owner,
        fencing_token=claim.fencing_token,
        operation_id=(
            f"manual-attach:{attachment.target.generation}:review-blocked"
        ),
    )
    _manual_release_existing_state(
        store,
        job_id=job_id,
        generation=attachment.target.generation,
        owner_id=lease_owner,
        fencing_token=claim.fencing_token,
        event_kind="manual_attachment_blocked",
        target_digest=attachment.target.target_digest,
        payload={"automatic_resume": True},
        operation_id=(
            f"manual-attach:{attachment.target.generation}:blocked-release:"
            f"{claim.fencing_token}"
        ),
    )
    _enqueue_attached_bestplan_resume(
        state_db_path=owner.state_db_path,
        job_id=job_id,
    )
    return _manual_result(
        conversation_history,
        response="The active BestPlan review found blockers and will repair them.",
        completed=False,
        api_calls=api_calls,
        job_id=job_id,
        state="blocked",
    )


def run_manual_review_request(
    agent: object,
    *,
    scope: str,
    conversation_history: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Review one immutable manual objective and repair it until both lanes pass."""

    owner = _manual_owner(agent)
    resume_job_id = getattr(agent, "_manual_review_resume_job_id", None)
    changed_paths: tuple[str, ...] | None = None
    if (
        resume_job_id is None
        and _manual_active_review_hint(conversation_history) is None
    ):
        changed_paths = _manual_objective_paths(owner=owner, scope=scope)
        if not changed_paths:
            return _manual_no_target(
                owner=owner,
                conversation_history=conversation_history,
            )

    runtime = getattr(agent, "manual_review_runtime", None)
    if runtime is None:
        runtime = _DefaultManualReviewRuntime(agent, owner.workspace)
    runtime_workspace = getattr(runtime, "workspace", owner.workspace)
    try:
        resolved_runtime_workspace = Path(runtime_workspace).expanduser().resolve(
            strict=True
        )
    except (TypeError, OSError, RuntimeError) as exc:
        raise ReviewValidationError("manual review runtime workspace is invalid") from exc
    if resolved_runtime_workspace != owner.workspace:
        raise ReviewRequiresAuthority(
            "manual review runtime does not own the selected workspace"
        )
    bindings = validate_reviewer_runtimes(getattr(runtime, "reviewer_bindings", ()))
    reviewer_call = getattr(runtime, "reviewer_call", None)
    repair_call = getattr(runtime, "repair_generation", None)
    check_call = getattr(runtime, "run_checks", None)
    if not all(callable(item) for item in (reviewer_call, repair_call, check_call)):
        raise ReviewValidationError("manual review runtime is incomplete")
    from agent.bestplan_review import bestplan_review_policy_digest

    policy_digest = bestplan_review_policy_digest(bindings)
    if resume_job_id is None:
        attachment = _manual_active_bestplan_attachment(
            owner=owner,
            conversation_history=conversation_history,
            policy_digest=policy_digest,
        )
        if attachment is not None:
            return _run_attached_bestplan_review(
                agent,
                owner=owner,
                attachment=attachment,
                runtime=runtime,
                bindings=bindings,
                reviewer_call=reviewer_call,
                conversation_history=conversation_history,
            )
        if changed_paths is None:
            changed_paths = _manual_objective_paths(owner=owner, scope=scope)
            if not changed_paths:
                return _manual_no_target(
                    owner=owner,
                    conversation_history=conversation_history,
                )
        task = _manual_task(conversation_history)
        acceptance = (
            "The selected objective satisfies the user's requested change.",
        )
        acceptance_digest = _domain_digest(
            _MANUAL_ACCEPTANCE_DOMAIN,
            _canonical_json(
                {
                    "acceptance": list(acceptance),
                    "changed_paths": list(changed_paths),
                    "task": task,
                }
            ),
        )
        capture = _capture_manual_snapshot(owner.workspace, changed_paths)
        job_id = _manual_job_id(
            owner,
            capture,
            identity_digest=_domain_digest(
                b"hermes.manual-review-job-identity.v1\0",
                _canonical_json(
                    {
                        "acceptance_digest": acceptance_digest,
                        "policy_digest": policy_digest,
                    }
                ),
            ),
        )
        generation = 0
        bundle = _manual_build_bundle(
            owner=owner,
            job_id=job_id,
            generation=generation,
            capture=capture,
            task=task,
            acceptance=acceptance,
            acceptance_digest=acceptance_digest,
            policy_digest=policy_digest,
            check_receipt_digest=_MANUAL_ZERO_DIGEST,
            dispositions=(),
        )
        job_adapter_state = dict(bundle.adapter_state)
        job_runtime_routes = _manual_runtime_routes(bindings)
    else:
        job_id = _require_text(resume_job_id, "manual resume job", maximum=256)
        resume_store = ReviewStore(owner.state_db_path)
        existing_job = resume_store.get_job(job_id)
        if (
            existing_job.adapter_version != _MANUAL_ADAPTER_VERSION
            or existing_job.source_kind != "manual_snapshot"
            or existing_job.owner_session_id != owner.session_id
            or existing_job.workspace != str(owner.workspace)
            or existing_job.current_generation is None
        ):
            raise ReviewStoreConflict("manual resume job identity differs")
        if existing_job.policy_digest != policy_digest:
            pending_generation = int(existing_job.current_generation) + 1
            passed_checks = resume_store.get_manual_checkpoint(
                job_id=job_id,
                generation=pending_generation,
                phase="checks_passed",
            )
            if existing_job.state != "checking" or passed_checks is None:
                raise ReviewStoreConflict("manual resume job identity differs")
            return _manual_result(
                conversation_history,
                response=(
                    "Manual review is waiting for its original reviewer "
                    "identities to become available."
                ),
                completed=False,
                api_calls=0,
                job_id=job_id,
                state="checking",
            )
        adapter_state = _strict_json_object(
            existing_job.adapter_state_json, "manual resume adapter state"
        )
        raw_paths = adapter_state.get("changed_paths")
        if not isinstance(raw_paths, list):
            raise ReviewStoreConflict("manual resume paths are invalid")
        changed_paths = tuple(
            sorted(_require_relative_path(item) for item in raw_paths)
        )
        generation = int(existing_job.current_generation)
        stored_generation = resume_store.get_generation(job_id, generation)
        if stored_generation.artifact_json is None:
            raise ReviewStoreConflict("manual resume artifact is missing")
        stored_target = _restore_review_target(stored_generation.target_json)
        stored_artifact = _restore_review_artifact(
            stored_generation.artifact_json,
            target=stored_target,
        )
        artifact_payload = _strict_json_object(
            stored_artifact.canonical_json, "manual resume artifact"
        )
        task = _require_text(artifact_payload.get("task"), "manual resume task")
        raw_acceptance = artifact_payload.get("acceptance")
        if not isinstance(raw_acceptance, list):
            raise ReviewStoreConflict("manual resume acceptance is invalid")
        acceptance = tuple(
            _require_text(item, "manual resume acceptance")
            for item in raw_acceptance
        )
        dispositions = artifact_payload.get("dispositions")
        if not isinstance(dispositions, list):
            raise ReviewStoreConflict("manual resume dispositions are invalid")
        acceptance_digest = stored_target.acceptance_digest
        capture = _restore_manual_capture(
            owner=owner,
            job_id=job_id,
            generation=generation,
            snapshot_digest=stored_target.snapshot_digest,
        )
        bundle = _manual_build_bundle(
            owner=owner,
            job_id=job_id,
            generation=generation,
            capture=capture,
            task=task,
            acceptance=acceptance,
            acceptance_digest=acceptance_digest,
            policy_digest=policy_digest,
            check_receipt_digest=stored_target.check_receipt_digest,
            dispositions=dispositions,
        )
        if (
            bundle.target.canonical_json != stored_target.canonical_json
            or bundle.artifact.canonical_json != stored_artifact.canonical_json
        ):
            raise ReviewStoreConflict("manual resume target changed")
        job_adapter_state = adapter_state
        job_runtime_routes = json.loads(existing_job.runtime_routes_json)
    store = ReviewStore(owner.state_db_path)
    job = store.create_job(
        job_id=job_id,
        source_kind=bundle.target.source_kind,
        source_id=bundle.target.plan_id,
        target_digest=bundle.target.target_digest,
        policy_digest=bundle.target.policy_digest,
        integration_oid=bundle.target.integration_oid,
        check_receipt_digest=bundle.target.check_receipt_digest,
        adapter_version=_MANUAL_ADAPTER_VERSION,
        owner_session_id=owner.session_id,
        owner_profile=owner.profile,
        workspace=str(owner.workspace),
        adapter_state=job_adapter_state,
        runtime_routes=job_runtime_routes,
        initial_target=bundle.target if resume_job_id is None else None,
        initial_artifact=bundle.artifact if resume_job_id is None else None,
    )
    lease_owner = "manual-" + secrets.token_hex(16)
    if job.cancel_requested:
        return _manual_result(
            conversation_history,
            response=_manual_cancel_response(job.state),
            completed=False,
            api_calls=0,
            job_id=job_id,
            state=job.state,
            interrupted=True,
        )
    try:
        claim = store.claim_job(
            job_id=job_id,
            owner_id=lease_owner,
            now_ns=time.time_ns(),
            lease_duration_ns=_MANUAL_REVIEW_LEASE_NS,
            expected_fencing_token=job.fencing_token,
        )
    except ReviewLeaseConflict:
        active = store.get_job(job_id)
        if active.cancel_requested:
            return _manual_result(
                conversation_history,
                response=_manual_cancel_response(active.state),
                completed=False,
                api_calls=0,
                job_id=job_id,
                state=active.state,
                interrupted=True,
            )
        return _manual_result(
            conversation_history,
            response="This manual review is already active in another invocation.",
            completed=False,
            api_calls=0,
            job_id=job_id,
            state=active.state,
        )

    api_calls = 0
    event_serial = len(store.list_events(job_id))
    cancel_event = threading.Event()
    adoption_lock = threading.Lock()

    def operation_id(kind: str) -> str:
        nonlocal event_serial
        event_serial += 1
        return (
            f"manual:{generation}:{kind}:{claim.fencing_token}:{event_serial}"
        )

    def interrupted_result() -> dict[str, object]:
        with adoption_lock:
            current = store.get_job(job_id)
            if current.cancel_requested:
                cancel_event.set()
            elif (
                current.owner_id == lease_owner
                and current.fencing_token == claim.fencing_token
            ):
                store.request_manual_cancel_intent(
                    job_id=job_id,
                    expected_target_digest=current.target_digest,
                    operation_id=operation_id("cancel_requested"),
                    signal_children=cancel_event.set,
                )
            else:
                store.request_manual_cancel_intent(
                    job_id=job_id,
                    expected_target_digest=current.target_digest,
                    operation_id=operation_id("cancel_requested_after_reclaim"),
                    signal_children=cancel_event.set,
                )
        cancelled_state = store.get_job(job_id).state
        return _manual_result(
            conversation_history,
            response=_manual_cancel_response(cancelled_state),
            completed=False,
            api_calls=api_calls,
            job_id=job_id,
            state=cancelled_state,
            interrupted=True,
        )

    adopted_receipts: tuple[StoredReviewerReceipt, ...] = ()
    resume_from_blocked = False
    if claim.current_generation is None:
        store.begin_generation(
            job_id=job_id,
            generation=generation,
            target=bundle.target,
            artifact=bundle.artifact,
            owner_id=lease_owner,
            fencing_token=claim.fencing_token,
            operation_id=f"manual:{generation}:generation_started",
        )
    else:
        generation = int(claim.current_generation)
        if generation != bundle.target.generation:
            raise ReviewStoreConflict(
                "manual review restart target generation is stale"
            )
        resume = store.resume_job(
            job_id=job_id,
            owner_id=lease_owner,
            fencing_token=claim.fencing_token,
        )
        adopted_receipts = resume.adopted_reviewer_receipts
        if resume.next_action == "handoff_pass":
            return _manual_result(
                conversation_history,
                response=(
                    "Manual review passed. Both independent reviewers found no "
                    "blocking issue in the exact objective snapshot."
                ),
                completed=True,
                api_calls=0,
                job_id=job_id,
                state="passed",
            )
        if resume.next_action in {
            "manual_reconcile",
            "manual_checks",
            "manual_begin_generation",
        }:
            next_generation = generation + 1
            prepared_checkpoint = store.get_manual_checkpoint(
                job_id=job_id,
                generation=next_generation,
                phase="repair_prepared",
            )
            if prepared_checkpoint is None:
                raise ReviewStoreConflict(
                    "manual recovery has no prepared repair checkpoint"
                )
            checkpoint_payload = _strict_json_object(
                prepared_checkpoint.payload_json,
                "manual recovery checkpoint",
            )
            frozen_check_receipt = _require_digest(
                checkpoint_payload.get("check_receipt_digest"),
                "manual recovery check receipt",
            )
            next_capture = _restore_manual_capture(
                owner=owner,
                job_id=job_id,
                generation=next_generation,
                snapshot_digest=prepared_checkpoint.snapshot_digest,
            )
            try:
                _manual_reconcile_repair_capture(
                    owner=owner,
                    prior_capture=capture,
                    repaired_capture=next_capture,
                    ambient_digest=_require_digest(
                        checkpoint_payload.get("ambient_digest"),
                        "manual recovery ambient digest",
                    ),
                    cancel_event=cancel_event,
                    adoption_lock=adoption_lock,
                )
            except ReviewStoreConflict as exc:
                _manual_transition(
                    store,
                    job_id=job_id,
                    generation=generation,
                    owner_id=lease_owner,
                    fencing_token=claim.fencing_token,
                    state="waiting",
                    event_kind="target_drift",
                    target_digest=bundle.target.target_digest,
                    payload={"reason": str(exc)},
                    operation_id=(
                        f"manual:{generation}:recovery-target-drift:"
                        f"{claim.fencing_token}"
                    ),
                    release_lease=True,
                )
                return _manual_result(
                    conversation_history,
                    response=(
                        "The manual review recovery target changed. "
                        "Review is waiting."
                    ),
                    completed=False,
                    api_calls=0,
                    job_id=job_id,
                    state="waiting",
                )
            applied_checkpoint = store.get_manual_checkpoint(
                job_id=job_id,
                generation=next_generation,
                phase="repair_applied",
            )
            if applied_checkpoint is None:
                applied_checkpoint = store.record_manual_checkpoint(
                    job_id=job_id,
                    prior_generation=generation,
                    generation=next_generation,
                    phase="repair_applied",
                    prior_target_digest=bundle.target.target_digest,
                    snapshot_digest=prepared_checkpoint.snapshot_digest,
                    live_state_digest=prepared_checkpoint.live_state_digest,
                    payload=checkpoint_payload,
                    owner_id=lease_owner,
                    fencing_token=claim.fencing_token,
                    operation_id=(
                        f"manual:{next_generation}:repair-applied:"
                        f"{claim.fencing_token}"
                    ),
                )
            checks_checkpoint = store.get_manual_checkpoint(
                job_id=job_id,
                generation=next_generation,
                phase="checks_passed",
            )
            restored_prior = {
                receipt.slot: _manual_restore_reviewer_receipt(
                    receipt, bundle=bundle
                )
                for receipt in adopted_receipts
            }
            prior_review_receipt = _manual_generation_receipt(
                bundle.target, tuple(restored_prior.values())
            )
            check_receipt_digest = frozen_check_receipt
            if checks_checkpoint is None:
                pending_check = (
                    checkpoint_payload.get("check_state") == "pending"
                    or frozen_check_receipt == _MANUAL_ZERO_DIGEST
                )
                if pending_check:
                    repair_paths = _manual_result_paths(
                        owner, checkpoint_payload.get("changed_paths")
                    )
                    if any(path not in changed_paths for path in repair_paths):
                        raise ReviewStoreConflict(
                            "manual pending check exceeds the objective"
                        )
                    pending_dispositions = tuple(
                        {
                            "evidence": (
                                f"Scoped repair generation {next_generation} "
                                "is waiting for its exact checks."
                            ),
                            "finding_fingerprint": finding.fingerprint,
                            "status": "fixed",
                        }
                        for finding in prior_review_receipt.blocking_findings
                    )
                    pending_bundle = _manual_build_bundle(
                        owner=owner,
                        job_id=job_id,
                        generation=next_generation,
                        capture=next_capture,
                        task=task,
                        acceptance=acceptance,
                        acceptance_digest=acceptance_digest,
                        policy_digest=policy_digest,
                        check_receipt_digest=frozen_check_receipt,
                        dispositions=pending_dispositions,
                    )
                    try:
                        check_result = _manual_controlled_call(
                            agent=agent,
                            store=store,
                            job_id=job_id,
                            owner_id=lease_owner,
                            fencing_token=claim.fencing_token,
                            cancel_operation_id=operation_id(
                                "cancel_requested"
                            ),
                            cancel_event=cancel_event,
                            adoption_lock=adoption_lock,
                            call=lambda: check_call(
                                changed_paths=repair_paths,
                                generation=next_generation,
                                target=pending_bundle.target,
                                workspace=owner.workspace,
                                cancel_event=cancel_event,
                                adoption_lock=adoption_lock,
                            ),
                        )
                    except _ManualInvocationCancelled:
                        return interrupted_result()
                    except Exception as exc:
                        check_result = {
                            "status": "waiting",
                            "error_type": type(exc).__name__,
                        }
                    if not isinstance(check_result, Mapping):
                        check_result = {"status": "waiting"}
                    check_status = str(
                        check_result.get("status") or ""
                    ).strip().casefold()
                    if check_status == "failed":
                        raise ReviewStoreConflict(
                            "recovered manual check failed deterministically"
                        )
                    try:
                        if check_status != "passed":
                            raise ReviewValidationError(
                                "manual recovered check is waiting"
                            )
                        check_receipt_digest = _require_digest(
                            check_result.get("receipt_digest"),
                            "manual recovered check receipt",
                        )
                    except ReviewValidationError:
                        _manual_transition(
                            store,
                            job_id=job_id,
                            generation=generation,
                            owner_id=lease_owner,
                            fencing_token=claim.fencing_token,
                            state="checking",
                            event_kind="checks_waiting",
                            target_digest=bundle.target.target_digest,
                            payload={"status": check_status or "invalid"},
                            operation_id=operation_id("checks_waiting"),
                            release_lease=True,
                        )
                        _enqueue_manual_review_resume(
                            state_db_path=owner.state_db_path,
                            job_id=job_id,
                        )
                        return _manual_result(
                            conversation_history,
                            response=(
                                "Manual checks are waiting for automatic recovery."
                            ),
                            completed=False,
                            api_calls=api_calls,
                            job_id=job_id,
                            state="checking",
                        )
                    checkpoint_payload = {
                        **checkpoint_payload,
                        "check_receipt_digest": check_receipt_digest,
                        "check_state": "passed",
                    }
                checks_checkpoint = store.record_manual_checkpoint(
                    job_id=job_id,
                    prior_generation=generation,
                    generation=next_generation,
                    phase="checks_passed",
                    prior_target_digest=bundle.target.target_digest,
                    snapshot_digest=prepared_checkpoint.snapshot_digest,
                    live_state_digest=prepared_checkpoint.live_state_digest,
                    payload=checkpoint_payload,
                    owner_id=lease_owner,
                    fencing_token=claim.fencing_token,
                    operation_id=operation_id("checks_passed"),
                )
            else:
                checks_payload = _strict_json_object(
                    checks_checkpoint.payload_json,
                    "manual recovered checks checkpoint",
                )
                check_receipt_digest = _require_digest(
                    checks_payload.get("check_receipt_digest"),
                    "manual recovered check receipt",
                )
            dispositions = tuple(
                {
                    "evidence": (
                        f"Scoped repair generation {next_generation} passed "
                        f"checks with receipt {check_receipt_digest}."
                    ),
                    "finding_fingerprint": finding.fingerprint,
                    "status": "fixed",
                }
                for finding in prior_review_receipt.blocking_findings
            )
            next_bundle = _manual_build_bundle(
                owner=owner,
                job_id=job_id,
                generation=next_generation,
                capture=next_capture,
                task=task,
                acceptance=acceptance,
                acceptance_digest=acceptance_digest,
                policy_digest=policy_digest,
                check_receipt_digest=check_receipt_digest,
                dispositions=dispositions,
            )
            store.begin_generation(
                job_id=job_id,
                generation=next_generation,
                target=next_bundle.target,
                artifact=next_bundle.artifact,
                owner_id=lease_owner,
                fencing_token=claim.fencing_token,
                operation_id=(
                    f"manual:{next_generation}:generation_started"
                ),
            )
            generation = next_generation
            capture = next_capture
            bundle = next_bundle
            adopted_receipts = ()
        elif resume.next_action == "repair":
            resume_from_blocked = True
        elif resume.next_action == "review_missing_slots":
            pass
        elif resume.next_action != "wait_for_host":
            raise ReviewStoreConflict(
                "manual review restart is not at a durable wait checkpoint"
            )
        else:
            _manual_transition(
                store,
                job_id=job_id,
                generation=generation,
                owner_id=lease_owner,
                fencing_token=claim.fencing_token,
                state="reviewing",
                event_kind="review_resumed",
                target_digest=bundle.target.target_digest,
                payload={
                    "adopted_slots": [item.slot for item in adopted_receipts]
                },
                operation_id=(
                    f"manual:{generation}:review_resumed:{claim.fencing_token}"
                ),
            )

    restored = {
        receipt.slot: _manual_restore_reviewer_receipt(receipt, bundle=bundle)
        for receipt in adopted_receipts
    }
    while True:
        receipts = dict(restored)
        restored = {}
        for binding in bindings:
            if binding.slot in receipts:
                continue
            inline_reviewer_failures = 0
            while True:
                if _manual_interrupt_requested(agent):
                    return interrupted_result()
                api_calls += 1
                try:
                    reviewer_receipt = _manual_controlled_call(
                        agent=agent,
                        store=store,
                        job_id=job_id,
                        owner_id=lease_owner,
                        fencing_token=claim.fencing_token,
                        cancel_operation_id=operation_id("cancel_requested"),
                        cancel_event=cancel_event,
                        adoption_lock=adoption_lock,
                        call=lambda: _manual_call_reviewer(
                            binding,
                            bundle=bundle,
                            reviewer_call=reviewer_call,
                        ),
                    )
                except _ManualInvocationCancelled:
                    return interrupted_result()
                except ReviewRequiresAuthority:
                    _manual_transition(
                        store,
                        job_id=job_id,
                        generation=generation,
                        owner_id=lease_owner,
                        fencing_token=claim.fencing_token,
                        state="blocked_requires_authority",
                        event_kind="blocked_requires_authority",
                        target_digest=bundle.target.target_digest,
                        payload={"reason": "review evidence exceeds authority"},
                        operation_id=operation_id("blocked_requires_authority"),
                    )
                    return _manual_result(
                        conversation_history,
                        response="Manual review needs authority outside the objective.",
                        completed=False,
                        api_calls=api_calls,
                        job_id=job_id,
                        state="blocked_requires_authority",
                    )
                except ConnectionError as exc:
                    _manual_transition(
                        store,
                        job_id=job_id,
                        generation=generation,
                        owner_id=lease_owner,
                        fencing_token=claim.fencing_token,
                        state="waiting",
                        event_kind="reviewer_failure",
                        target_digest=bundle.target.target_digest,
                        payload={"error_type": type(exc).__name__},
                        operation_id=operation_id("reviewer_failure"),
                        release_lease=True,
                    )
                    _enqueue_manual_review_resume(
                        state_db_path=owner.state_db_path,
                        job_id=job_id,
                    )
                    return _manual_result(
                        conversation_history,
                        response=(
                            "Manual review saved its reviewer receipt and is "
                            "waiting for the review host to resume."
                        ),
                        completed=False,
                        api_calls=api_calls,
                        job_id=job_id,
                        state="waiting",
                    )
                except Exception as exc:
                    inline_reviewer_failures += 1
                    if inline_reviewer_failures > 1:
                        _manual_transition(
                            store,
                            job_id=job_id,
                            generation=generation,
                            owner_id=lease_owner,
                            fencing_token=claim.fencing_token,
                            state="waiting",
                            event_kind="reviewer_failure",
                            target_digest=bundle.target.target_digest,
                            payload={"error_type": type(exc).__name__},
                            operation_id=operation_id("reviewer_failure"),
                            release_lease=True,
                        )
                        _enqueue_manual_review_resume(
                            state_db_path=owner.state_db_path,
                            job_id=job_id,
                        )
                        return _manual_result(
                            conversation_history,
                            response=(
                                "Manual review is waiting for automatic "
                                "reviewer recovery."
                            ),
                            completed=False,
                            api_calls=api_calls,
                            job_id=job_id,
                            state="waiting",
                        )
                    _manual_transition(
                        store,
                        job_id=job_id,
                        generation=generation,
                        owner_id=lease_owner,
                        fencing_token=claim.fencing_token,
                        state="reviewing",
                        event_kind="reviewer_failure",
                        target_digest=bundle.target.target_digest,
                        payload={"error_type": type(exc).__name__},
                        operation_id=operation_id("reviewer_failure"),
                    )
                    refresh = getattr(runtime, "refresh_reviewers", None)
                    if callable(refresh):
                        refresh()
                        refreshed = validate_reviewer_runtimes(
                            getattr(runtime, "reviewer_bindings", ())
                        )
                        if bestplan_review_policy_digest(refreshed) != policy_digest:
                            raise ReviewStoreConflict(
                                "manual reviewer identity changed during retry"
                            )
                    continue
                receipt_json = _manual_reviewer_receipt_json(reviewer_receipt)
                store.record_reviewer_receipt(
                    job_id=job_id,
                    generation=generation,
                    slot=reviewer_receipt.slot,
                    target_digest=bundle.target.target_digest,
                    integration_oid=bundle.target.integration_oid,
                    output_digest=reviewer_receipt.output_digest,
                    verdict_digest=_domain_digest(
                        _MANUAL_VERDICT_DOMAIN, receipt_json
                    ),
                    passed=reviewer_receipt.verdict.passed,
                    receipt_json=receipt_json,
                    owner_id=lease_owner,
                    fencing_token=claim.fencing_token,
                    operation_id=(
                        f"manual:{generation}:reviewer:{reviewer_receipt.slot}"
                    ),
                )
                receipts[reviewer_receipt.slot] = reviewer_receipt
                break
            if _manual_interrupt_requested(agent):
                return interrupted_result()
        review_receipt = _manual_generation_receipt(
            bundle.target, tuple(receipts.values())
        )
        if not _manual_target_is_current(owner, capture):
            _manual_transition(
                store,
                job_id=job_id,
                generation=generation,
                owner_id=lease_owner,
                fencing_token=claim.fencing_token,
                state="waiting",
                event_kind="target_drift",
                target_digest=bundle.target.target_digest,
                payload={"reason": "the live objective differs from the snapshot"},
                operation_id=operation_id("target_drift"),
                release_lease=True,
            )
            return _manual_result(
                conversation_history,
                response=(
                    "The manual review stopped because the target changed after "
                    "snapshot capture. Run /review again for the new target."
                ),
                completed=False,
                api_calls=api_calls,
                job_id=job_id,
                state="waiting",
            )
        if review_receipt.passed:
            store.record_generation_pass(
                job_id=job_id,
                generation=generation,
                target_digest=bundle.target.target_digest,
                integration_oid=bundle.target.integration_oid,
                check_receipt_digest=bundle.target.check_receipt_digest,
                review_receipt_digest=review_receipt.receipt_digest,
                owner_id=lease_owner,
                fencing_token=claim.fencing_token,
                operation_id=f"manual:{generation}:review_pass",
            )
            return _manual_result(
                conversation_history,
                response=(
                    "Manual review passed. Both independent reviewers found no "
                    "blocking issue in the exact objective snapshot."
                ),
                completed=True,
                api_calls=api_calls,
                job_id=job_id,
                state="passed",
            )
        blocker_payload = [
            _manual_finding_payload(item) for item in review_receipt.blocking_findings
        ]
        if not resume_from_blocked:
            store.record_generation_blocked(
                job_id=job_id,
                generation=generation,
                target_digest=bundle.target.target_digest,
                integration_oid=bundle.target.integration_oid,
                check_receipt_digest=bundle.target.check_receipt_digest,
                review_receipt_digest=review_receipt.receipt_digest,
                blocking_findings_json=_canonical_json(blocker_payload),
                owner_id=lease_owner,
                fencing_token=claim.fencing_token,
                operation_id=f"manual:{generation}:review_blocked",
            )
        resume_from_blocked = False
        repair_findings = review_receipt.blocking_findings
        repair_target = bundle.target
        repair_capture = capture
        while True:
            repair_attempt = store.allocate_manual_repair_attempt(
                job_id=job_id,
                generation=generation,
                target_digest=bundle.target.target_digest,
                owner_id=lease_owner,
                fencing_token=claim.fencing_token,
                operation_id=operation_id("repair_attempt_allocated"),
            )
            ambient_before = _manual_ambient_digest(owner.workspace, changed_paths)
            repair_checkpoint: dict[str, object] = {}

            def checkpoint_repair(
                raw_checkpoint: Mapping[str, object],
            ) -> None:
                """Freeze exact repair evidence before or just after adoption."""

                if not isinstance(raw_checkpoint, Mapping):
                    raise ReviewValidationError(
                        "manual repair checkpoint is invalid"
                    )
                checkpoint_status = str(
                    raw_checkpoint.get("status") or ""
                ).strip().casefold()
                if checkpoint_status not in {"prepared", "applied"}:
                    raise ReviewValidationError(
                        "manual repair checkpoint status is invalid"
                    )
                checkpoint_paths = _manual_result_paths(
                    owner, raw_checkpoint.get("changed_paths")
                )
                if (
                    not checkpoint_paths
                    or any(path not in changed_paths for path in checkpoint_paths)
                ):
                    raise ReviewRequiresAuthority(
                        "manual repair checkpoint exceeds the objective"
                    )
                checkpoint_receipt = _require_digest(
                    raw_checkpoint.get("check_receipt_digest"),
                    "manual repair checkpoint receipt",
                )
                raw_tree = raw_checkpoint.get("snapshot_tree_oid")
                if raw_tree is not None:
                    checkpoint_capture = _capture_manual_tree_snapshot(
                        owner.workspace,
                        changed_paths,
                        snapshot_tree_oid=_require_oid(
                            raw_tree, "manual repair checkpoint tree"
                        ),
                    )
                elif checkpoint_status == "applied":
                    checkpoint_capture = _capture_manual_snapshot(
                        owner.workspace, changed_paths
                    )
                else:
                    raise ReviewValidationError(
                        "prepared manual repair has no frozen tree"
                    )
                next_generation = generation + 1
                checkpoint_dispositions = tuple(
                    {
                        "evidence": (
                            f"Scoped repair generation {next_generation} passed "
                            f"checks with receipt {checkpoint_receipt}."
                        ),
                        "finding_fingerprint": finding.fingerprint,
                        "status": "fixed",
                    }
                    for finding in review_receipt.blocking_findings
                )
                checkpoint_bundle = _manual_build_bundle(
                    owner=owner,
                    job_id=job_id,
                    generation=next_generation,
                    capture=checkpoint_capture,
                    task=task,
                    acceptance=acceptance,
                    acceptance_digest=acceptance_digest,
                    policy_digest=policy_digest,
                    check_receipt_digest=checkpoint_receipt,
                    dispositions=checkpoint_dispositions,
                )
                payload = {
                    "ambient_digest": ambient_before,
                    "changed_paths": list(checkpoint_paths),
                    "check_receipt_digest": checkpoint_receipt,
                    "integration_oid": str(
                        raw_checkpoint.get("integration_oid") or ""
                    ),
                    "integration_receipt_digest": str(
                        raw_checkpoint.get("integration_receipt_digest") or ""
                    ),
                    "integration_ref": str(
                        raw_checkpoint.get("integration_ref") or ""
                    ),
                    "schema": "hermes.manual-review-checkpoint.v1",
                }
                prepared = store.record_manual_checkpoint(
                    job_id=job_id,
                    prior_generation=generation,
                    generation=next_generation,
                    phase="repair_prepared",
                    prior_target_digest=bundle.target.target_digest,
                    snapshot_digest=checkpoint_bundle.target.snapshot_digest,
                    live_state_digest=checkpoint_capture.live_state_digest,
                    payload=payload,
                    owner_id=lease_owner,
                    fencing_token=claim.fencing_token,
                    operation_id=operation_id("repair_prepared"),
                )
                repair_checkpoint.update(
                    {
                        "bundle": checkpoint_bundle,
                        "capture": checkpoint_capture,
                        "payload": payload,
                        "prepared": prepared,
                    }
                )
                if checkpoint_status == "applied":
                    if (
                        not _manual_target_is_current(owner, checkpoint_capture)
                        or not hmac.compare_digest(
                            ambient_before,
                            _manual_ambient_digest(
                                owner.workspace, changed_paths
                            ),
                        )
                    ):
                        raise ReviewStoreConflict(
                            "manual repair changed after checkpoint adoption"
                        )
                    applied = store.record_manual_checkpoint(
                        job_id=job_id,
                        prior_generation=generation,
                        generation=next_generation,
                        phase="repair_applied",
                        prior_target_digest=bundle.target.target_digest,
                        snapshot_digest=prepared.snapshot_digest,
                        live_state_digest=prepared.live_state_digest,
                        payload=payload,
                        owner_id=lease_owner,
                        fencing_token=claim.fencing_token,
                        operation_id=operation_id("repair_applied"),
                    )
                    checks = store.record_manual_checkpoint(
                        job_id=job_id,
                        prior_generation=generation,
                        generation=next_generation,
                        phase="checks_passed",
                        prior_target_digest=bundle.target.target_digest,
                        snapshot_digest=applied.snapshot_digest,
                        live_state_digest=applied.live_state_digest,
                        payload=payload,
                        owner_id=lease_owner,
                        fencing_token=claim.fencing_token,
                        operation_id=operation_id("checks_passed"),
                    )
                    repair_checkpoint["applied"] = applied
                    repair_checkpoint["checks"] = checks

            _manual_transition(
                store,
                job_id=job_id,
                generation=generation,
                owner_id=lease_owner,
                fencing_token=claim.fencing_token,
                state="repairing",
                event_kind="repair_started",
                target_digest=bundle.target.target_digest,
                payload={
                    "attempt": repair_attempt,
                    "next_generation": generation + 1,
                },
                operation_id=operation_id("repair_started"),
            )
            try:
                repair_result = _manual_controlled_call(
                    agent=agent,
                    store=store,
                    job_id=job_id,
                    owner_id=lease_owner,
                    fencing_token=claim.fencing_token,
                    cancel_operation_id=operation_id("cancel_requested"),
                    cancel_event=cancel_event,
                    adoption_lock=adoption_lock,
                    call=lambda: repair_call(
                        blockers=repair_findings,
                        allowed_paths=changed_paths,
                        generation=generation + 1,
                        repair_attempt=repair_attempt,
                        target=repair_target,
                        workspace=owner.workspace,
                        expected_live_state_digest=repair_capture.live_state_digest,
                        task=task,
                        cancel_event=cancel_event,
                        adoption_lock=adoption_lock,
                        checkpoint_callback=checkpoint_repair,
                    ),
                )
            except _ManualInvocationCancelled:
                return interrupted_result()
            except ReviewRequiresAuthority:
                repair_result = {"status": "requires_authority"}
            except Exception as exc:
                _manual_transition(
                    store,
                    job_id=job_id,
                    generation=generation,
                    owner_id=lease_owner,
                    fencing_token=claim.fencing_token,
                    state="waiting",
                    event_kind="repair_waiting",
                    target_digest=bundle.target.target_digest,
                    payload={"error_type": type(exc).__name__},
                    operation_id=operation_id("repair_waiting"),
                    release_lease=True,
                )
                _enqueue_manual_review_resume(
                    state_db_path=owner.state_db_path,
                    job_id=job_id,
                )
                return _manual_result(
                    conversation_history,
                    response="Manual repair is waiting for automatic recovery.",
                    completed=False,
                    api_calls=api_calls,
                    job_id=job_id,
                    state="waiting",
                )
            if not isinstance(repair_result, Mapping):
                repair_result = {
                    "status": "waiting",
                    "reason": "invalid repair result",
                }
            status_value = str(
                repair_result.get("status") or ""
            ).strip().casefold()
            requested_paths: tuple[str, ...] = ()
            if repair_result.get("requested_paths") is not None:
                try:
                    requested_paths = _manual_result_paths(
                        owner, repair_result.get("requested_paths")
                    )
                except ReviewValidationError:
                    requested_paths = ("<invalid-or-outside>",)
            if status_value == "requires_authority" or any(
                path not in changed_paths for path in requested_paths
            ):
                _manual_transition(
                    store,
                    job_id=job_id,
                    generation=generation,
                    owner_id=lease_owner,
                    fencing_token=claim.fencing_token,
                    state="blocked_requires_authority",
                    event_kind="blocked_requires_authority",
                    target_digest=bundle.target.target_digest,
                    payload={"requested_paths": list(requested_paths)},
                    operation_id=operation_id("blocked_requires_authority"),
                )
                return _manual_result(
                    conversation_history,
                    response=(
                        "Manual review needs authority outside the selected "
                        "objective. No additional repair or check was run."
                    ),
                    completed=False,
                    api_calls=api_calls,
                    job_id=job_id,
                    state="blocked_requires_authority",
                )
            if status_value != "applied":
                _manual_transition(
                    store,
                    job_id=job_id,
                    generation=generation,
                    owner_id=lease_owner,
                    fencing_token=claim.fencing_token,
                    state="waiting",
                    event_kind="repair_waiting",
                    target_digest=bundle.target.target_digest,
                    payload={"status": status_value or "invalid"},
                    operation_id=operation_id("repair_waiting"),
                    release_lease=True,
                )
                _enqueue_manual_review_resume(
                    state_db_path=owner.state_db_path,
                    job_id=job_id,
                )
                return _manual_result(
                    conversation_history,
                    response="Manual repair is waiting for automatic recovery.",
                    completed=False,
                    api_calls=api_calls,
                    job_id=job_id,
                    state="waiting",
                )
            try:
                repair_paths = _manual_result_paths(
                    owner, repair_result.get("changed_paths")
                )
            except ReviewValidationError:
                repair_paths = ("<invalid-or-outside>",)
            if any(path not in changed_paths for path in repair_paths):
                _manual_transition(
                    store,
                    job_id=job_id,
                    generation=generation,
                    owner_id=lease_owner,
                    fencing_token=claim.fencing_token,
                    state="blocked_requires_authority",
                    event_kind="blocked_requires_authority",
                    target_digest=bundle.target.target_digest,
                    payload={"changed_paths": list(repair_paths)},
                    operation_id=operation_id("blocked_requires_authority"),
                )
                return _manual_result(
                    conversation_history,
                    response="Manual repair crossed its authority boundary.",
                    completed=False,
                    api_calls=api_calls,
                    job_id=job_id,
                    state="blocked_requires_authority",
                )
            if not hmac.compare_digest(
                ambient_before,
                _manual_ambient_digest(owner.workspace, changed_paths),
            ):
                _manual_transition(
                    store,
                    job_id=job_id,
                    generation=generation,
                    owner_id=lease_owner,
                    fencing_token=claim.fencing_token,
                    state="blocked_requires_authority",
                    event_kind="blocked_requires_authority",
                    target_digest=bundle.target.target_digest,
                    payload={"reason": "repair changed an unowned path"},
                    operation_id=operation_id("blocked_requires_authority"),
                )
                return _manual_result(
                    conversation_history,
                    response="Manual repair changed work outside its authority.",
                    completed=False,
                    api_calls=api_calls,
                    job_id=job_id,
                    state="blocked_requires_authority",
                )
            repaired_digest = _manual_live_state_digest(
                owner.workspace, changed_paths
            )
            if hmac.compare_digest(
                repaired_digest, repair_capture.live_state_digest
            ):
                _manual_transition(
                    store,
                    job_id=job_id,
                    generation=generation,
                    owner_id=lease_owner,
                    fencing_token=claim.fencing_token,
                    state="repairing",
                    event_kind="repair_no_progress",
                    target_digest=bundle.target.target_digest,
                    payload={
                        "attempt": repair_attempt,
                        "reason": "repair did not change the objective",
                    },
                    operation_id=operation_id("repair_no_progress"),
                )
                continue
            prepared = repair_checkpoint.get("prepared")
            checkpoint_capture = repair_checkpoint.get("capture")
            checkpoint_payload = repair_checkpoint.get("payload")
            if prepared is not None:
                if (
                    not isinstance(prepared, StoredManualCheckpoint)
                    or not isinstance(checkpoint_capture, _ManualCapture)
                    or not isinstance(checkpoint_payload, Mapping)
                    or repaired_digest != checkpoint_capture.live_state_digest
                    or tuple(repair_paths)
                    != tuple(checkpoint_payload.get("changed_paths", ()))
                ):
                    raise ReviewStoreConflict(
                        "manual repair result differs from its checkpoint"
                    )
                if repair_checkpoint.get("applied") is None:
                    repair_checkpoint["applied"] = store.record_manual_checkpoint(
                        job_id=job_id,
                        prior_generation=generation,
                        generation=generation + 1,
                        phase="repair_applied",
                        prior_target_digest=bundle.target.target_digest,
                        snapshot_digest=prepared.snapshot_digest,
                        live_state_digest=prepared.live_state_digest,
                        payload=checkpoint_payload,
                        owner_id=lease_owner,
                        fencing_token=claim.fencing_token,
                        operation_id=operation_id("repair_applied"),
                    )

            def ensure_repaired_checkpoint(
                check_receipt_digest: str,
                *,
                check_state: str,
            ) -> tuple[
                StoredManualCheckpoint,
                StoredManualCheckpoint,
                _ManualCapture,
                Mapping[str, object],
            ]:
                """Freeze the applied repair before a transient check wait."""

                prepared_checkpoint = repair_checkpoint.get("prepared")
                applied_checkpoint = repair_checkpoint.get("applied")
                frozen_capture = repair_checkpoint.get("capture")
                frozen_payload = repair_checkpoint.get("payload")
                if prepared_checkpoint is None:
                    frozen_capture = _capture_manual_snapshot(
                        owner.workspace, changed_paths
                    )
                    checkpoint_bundle = _manual_build_bundle(
                        owner=owner,
                        job_id=job_id,
                        generation=generation + 1,
                        capture=frozen_capture,
                        task=task,
                        acceptance=acceptance,
                        acceptance_digest=acceptance_digest,
                        policy_digest=policy_digest,
                        check_receipt_digest=check_receipt_digest,
                        dispositions=(),
                    )
                    frozen_payload = {
                        "ambient_digest": ambient_before,
                        "changed_paths": list(repair_paths),
                        "check_receipt_digest": check_receipt_digest,
                        "check_state": check_state,
                        "integration_oid": "",
                        "integration_receipt_digest": "",
                        "integration_ref": "",
                        "schema": "hermes.manual-review-checkpoint.v1",
                    }
                    prepared_checkpoint = store.record_manual_checkpoint(
                        job_id=job_id,
                        prior_generation=generation,
                        generation=generation + 1,
                        phase="repair_prepared",
                        prior_target_digest=bundle.target.target_digest,
                        snapshot_digest=checkpoint_bundle.target.snapshot_digest,
                        live_state_digest=frozen_capture.live_state_digest,
                        payload=frozen_payload,
                        owner_id=lease_owner,
                        fencing_token=claim.fencing_token,
                        operation_id=operation_id("repair_prepared"),
                    )
                    repair_checkpoint.update(
                        {
                            "bundle": checkpoint_bundle,
                            "capture": frozen_capture,
                            "payload": frozen_payload,
                            "prepared": prepared_checkpoint,
                        }
                    )
                if (
                    not isinstance(prepared_checkpoint, StoredManualCheckpoint)
                    or not isinstance(frozen_capture, _ManualCapture)
                    or not isinstance(frozen_payload, Mapping)
                ):
                    raise ReviewStoreConflict(
                        "manual repair prepared checkpoint is invalid"
                    )
                if applied_checkpoint is None:
                    applied_checkpoint = store.record_manual_checkpoint(
                        job_id=job_id,
                        prior_generation=generation,
                        generation=generation + 1,
                        phase="repair_applied",
                        prior_target_digest=bundle.target.target_digest,
                        snapshot_digest=prepared_checkpoint.snapshot_digest,
                        live_state_digest=prepared_checkpoint.live_state_digest,
                        payload=frozen_payload,
                        owner_id=lease_owner,
                        fencing_token=claim.fencing_token,
                        operation_id=operation_id("repair_applied"),
                    )
                    repair_checkpoint["applied"] = applied_checkpoint
                if not isinstance(applied_checkpoint, StoredManualCheckpoint):
                    raise ReviewStoreConflict(
                        "manual repair applied checkpoint is invalid"
                    )
                return (
                    prepared_checkpoint,
                    applied_checkpoint,
                    frozen_capture,
                    frozen_payload,
                )

            _manual_transition(
                store,
                job_id=job_id,
                generation=generation,
                owner_id=lease_owner,
                fencing_token=claim.fencing_token,
                state="checking",
                event_kind="checks_started",
                target_digest=bundle.target.target_digest,
                payload={"changed_paths": list(repair_paths)},
                operation_id=operation_id("checks_started"),
            )
            check_failed = False
            while True:
                try:
                    check_result = _manual_controlled_call(
                        agent=agent,
                        store=store,
                        job_id=job_id,
                        owner_id=lease_owner,
                        fencing_token=claim.fencing_token,
                        cancel_operation_id=operation_id("cancel_requested"),
                        cancel_event=cancel_event,
                        adoption_lock=adoption_lock,
                        call=lambda: check_call(
                            changed_paths=repair_paths,
                            generation=generation + 1,
                            target=repair_target,
                            workspace=owner.workspace,
                            cancel_event=cancel_event,
                            adoption_lock=adoption_lock,
                        ),
                    )
                except _ManualInvocationCancelled:
                    return interrupted_result()
                except Exception as exc:
                    check_result = {
                        "status": "waiting",
                        "error_type": type(exc).__name__,
                    }
                if not isinstance(check_result, Mapping):
                    check_result = {"status": "waiting"}
                check_status = str(
                    check_result.get("status") or ""
                ).strip().casefold()
                if check_status == "failed":
                    reason = str(
                        check_result.get("reason")
                        or "the deterministic objective check failed"
                    )
                    _manual_transition(
                        store,
                        job_id=job_id,
                        generation=generation,
                        owner_id=lease_owner,
                        fencing_token=claim.fencing_token,
                        state="repairing",
                        event_kind="checks_failed",
                        target_digest=bundle.target.target_digest,
                        payload={"reason": reason},
                        operation_id=operation_id("checks_failed"),
                    )
                    repair_capture = _capture_manual_snapshot(
                        owner.workspace, changed_paths
                    )
                    repair_bundle = _manual_build_bundle(
                        owner=owner,
                        job_id=job_id,
                        generation=generation,
                        capture=repair_capture,
                        task=task,
                        acceptance=acceptance,
                        acceptance_digest=acceptance_digest,
                        policy_digest=policy_digest,
                        check_receipt_digest=bundle.target.check_receipt_digest,
                        dispositions=(),
                    )
                    repair_target = repair_bundle.target
                    repair_findings = (
                        _manual_check_failure_finding(
                            target=repair_target,
                            path=repair_paths[0],
                            reason=reason,
                            attempt=repair_attempt,
                        ),
                    )
                    check_failed = True
                    break
                if check_status != "passed":
                    pending_receipt = _MANUAL_ZERO_DIGEST
                    raw_repair_receipt = repair_result.get(
                        "check_receipt_digest"
                    )
                    if isinstance(raw_repair_receipt, str):
                        try:
                            pending_receipt = _require_digest(
                                raw_repair_receipt,
                                "manual pending check receipt",
                            )
                        except ReviewValidationError:
                            pending_receipt = _MANUAL_ZERO_DIGEST
                    ensure_repaired_checkpoint(
                        pending_receipt,
                        check_state="pending",
                    )
                    _manual_transition(
                        store,
                        job_id=job_id,
                        generation=generation,
                        owner_id=lease_owner,
                        fencing_token=claim.fencing_token,
                        state="checking",
                        event_kind="checks_waiting",
                        target_digest=bundle.target.target_digest,
                        payload={"status": check_status or "invalid"},
                        operation_id=operation_id("checks_waiting"),
                        release_lease=True,
                    )
                    _enqueue_manual_review_resume(
                        state_db_path=owner.state_db_path,
                        job_id=job_id,
                    )
                    return _manual_result(
                        conversation_history,
                        response="Manual checks are waiting for automatic recovery.",
                        completed=False,
                        api_calls=api_calls,
                        job_id=job_id,
                        state="checking",
                    )
                try:
                    check_receipt_digest = _require_digest(
                        check_result.get("receipt_digest"),
                        "manual check receipt",
                    )
                except ReviewValidationError:
                    ensure_repaired_checkpoint(
                        _MANUAL_ZERO_DIGEST,
                        check_state="pending",
                    )
                    _manual_transition(
                        store,
                        job_id=job_id,
                        generation=generation,
                        owner_id=lease_owner,
                        fencing_token=claim.fencing_token,
                        state="checking",
                        event_kind="checks_waiting",
                        target_digest=bundle.target.target_digest,
                        payload={"status": "invalid_receipt"},
                        operation_id=operation_id("checks_waiting"),
                        release_lease=True,
                    )
                    _enqueue_manual_review_resume(
                        state_db_path=owner.state_db_path,
                        job_id=job_id,
                    )
                    return _manual_result(
                        conversation_history,
                        response="Manual checks are waiting for automatic recovery.",
                        completed=False,
                        api_calls=api_calls,
                        job_id=job_id,
                        state="checking",
                    )
                (
                    prepared,
                    applied,
                    checkpoint_capture,
                    checkpoint_payload,
                ) = ensure_repaired_checkpoint(
                    check_receipt_digest,
                    check_state="frozen",
                )
                frozen_receipt = _require_digest(
                    checkpoint_payload.get("check_receipt_digest"),
                    "manual repair frozen check receipt",
                )
                if frozen_receipt not in {
                    _MANUAL_ZERO_DIGEST,
                    check_receipt_digest,
                }:
                    raise ReviewStoreConflict(
                        "manual repair check receipt differs from its checkpoint"
                    )
                if repair_checkpoint.get("checks") is None:
                    passed_payload = {
                        **dict(checkpoint_payload),
                        "check_receipt_digest": check_receipt_digest,
                        "check_state": "passed",
                    }
                    repair_checkpoint["checks"] = store.record_manual_checkpoint(
                        job_id=job_id,
                        prior_generation=generation,
                        generation=generation + 1,
                        phase="checks_passed",
                        prior_target_digest=bundle.target.target_digest,
                        snapshot_digest=applied.snapshot_digest,
                        live_state_digest=applied.live_state_digest,
                        payload=passed_payload,
                        owner_id=lease_owner,
                        fencing_token=claim.fencing_token,
                        operation_id=operation_id("checks_passed"),
                    )
                break
            if check_failed:
                continue
            break
        if not hmac.compare_digest(
            ambient_before,
            _manual_ambient_digest(owner.workspace, changed_paths),
        ):
            _manual_transition(
                store,
                job_id=job_id,
                generation=generation,
                owner_id=lease_owner,
                fencing_token=claim.fencing_token,
                state="blocked_requires_authority",
                event_kind="blocked_requires_authority",
                target_digest=bundle.target.target_digest,
                payload={"reason": "checks changed an unowned path"},
                operation_id=operation_id("blocked_requires_authority"),
            )
            return _manual_result(
                conversation_history,
                response="Manual checks changed work outside their authority.",
                completed=False,
                api_calls=api_calls,
                job_id=job_id,
                state="blocked_requires_authority",
            )
        try:
            refresh = getattr(runtime, "refresh_reviewers", None)
            if callable(refresh):
                refresh()
            bindings = validate_reviewer_runtimes(
                getattr(runtime, "reviewer_bindings", ())
            )
            reviewer_identity_matches = (
                bestplan_review_policy_digest(bindings) == policy_digest
            )
            reviewer_identity_error = ""
        except Exception as exc:
            reviewer_identity_matches = False
            reviewer_identity_error = type(exc).__name__
        if not reviewer_identity_matches:
            _manual_transition(
                store,
                job_id=job_id,
                generation=generation,
                owner_id=lease_owner,
                fencing_token=claim.fencing_token,
                state="checking",
                event_kind="reviewer_identity_drift",
                target_digest=bundle.target.target_digest,
                payload={
                    "error_type": reviewer_identity_error,
                    "reason": "reviewer identities changed",
                },
                operation_id=operation_id("reviewer_identity_drift"),
                release_lease=True,
            )
            _enqueue_manual_review_resume(
                state_db_path=owner.state_db_path,
                job_id=job_id,
            )
            return _manual_result(
                conversation_history,
                response=(
                    "Manual review is waiting for its original reviewer "
                    "identities to become available."
                ),
                completed=False,
                api_calls=api_calls,
                job_id=job_id,
                state="checking",
            )
        next_generation = generation + 1
        try:
            next_capture = _capture_manual_snapshot(
                owner.workspace, changed_paths
            )
        except ReviewValidationError as exc:
            _manual_transition(
                store,
                job_id=job_id,
                generation=generation,
                owner_id=lease_owner,
                fencing_token=claim.fencing_token,
                state="checking",
                event_kind="fresh_snapshot_failed",
                target_digest=bundle.target.target_digest,
                payload={"error_type": type(exc).__name__},
                operation_id=operation_id("fresh_snapshot_failed"),
                release_lease=True,
            )
            _enqueue_manual_review_resume(
                state_db_path=owner.state_db_path,
                job_id=job_id,
            )
            return _manual_result(
                conversation_history,
                response="Manual snapshot capture is waiting for automatic recovery.",
                completed=False,
                api_calls=api_calls,
                job_id=job_id,
                state="checking",
            )
        disposition_by_fingerprint = {
            finding.fingerprint: {
                "evidence": (
                    f"Scoped repair generation {next_generation} passed checks "
                    f"with receipt {check_receipt_digest}."
                ),
                "finding_fingerprint": finding.fingerprint,
                "status": "fixed",
            }
            for finding in review_receipt.blocking_findings
        }
        next_bundle = _manual_build_bundle(
            owner=owner,
            job_id=job_id,
            generation=next_generation,
            capture=next_capture,
            task=task,
            acceptance=acceptance,
            acceptance_digest=acceptance_digest,
            policy_digest=policy_digest,
            check_receipt_digest=check_receipt_digest,
            dispositions=tuple(disposition_by_fingerprint.values()),
        )
        store.begin_generation(
            job_id=job_id,
            generation=next_generation,
            target=next_bundle.target,
            artifact=next_bundle.artifact,
            owner_id=lease_owner,
            fencing_token=claim.fencing_token,
            operation_id=f"manual:{next_generation}:generation_started",
        )
        generation = next_generation
        capture = next_capture
        bundle = next_bundle


@dataclass(frozen=True)
class ReviewJournalEvent:
    plan_id: str
    event_seq: int
    generation: int
    operation_id: str
    kind: str
    target_digest: str
    integration_oid: str
    payload_json: str
    payload_digest: str
    previous_event_digest: str | None
    event_digest: str


class ReviewJournal:
    """Small append-only, hash-chained journal for exact review evidence."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_events (
                    plan_id TEXT NOT NULL,
                    event_seq INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    operation_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    target_digest TEXT NOT NULL,
                    integration_oid TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    previous_event_digest TEXT,
                    event_digest TEXT NOT NULL,
                    PRIMARY KEY (plan_id, event_seq),
                    UNIQUE (plan_id, operation_id),
                    UNIQUE (event_digest)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_review_events_exact_pass
                ON review_events (plan_id, target_digest, kind, event_seq)
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ReviewJournalEvent:
        return ReviewJournalEvent(**dict(row))

    def append(
        self,
        *,
        plan_id: str,
        generation: int,
        operation_id: str,
        kind: str,
        target_digest: str,
        integration_oid: str,
        payload: object,
    ) -> ReviewJournalEvent:
        plan_id = _require_text(plan_id, "plan_id", maximum=256)
        operation_id = _require_text(operation_id, "operation_id", maximum=256)
        kind = _require_text(kind, "kind", maximum=128)
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ReviewValidationError("generation must be a non-negative integer")
        target_digest = _require_digest(target_digest, "target_digest")
        integration_oid = _require_oid(integration_oid, "integration_oid")
        payload_json = _canonical_json(payload)
        payload_digest = _domain_digest(_EVENT_PAYLOAD_DOMAIN, payload_json)

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing_row = connection.execute(
                """
                SELECT * FROM review_events
                WHERE plan_id = ? AND operation_id = ?
                """,
                (plan_id, operation_id),
            ).fetchone()
            if existing_row is not None:
                existing = self._from_row(existing_row)
                expected = (
                    generation,
                    kind,
                    target_digest,
                    integration_oid,
                    payload_json,
                    payload_digest,
                )
                actual = (
                    existing.generation,
                    existing.kind,
                    existing.target_digest,
                    existing.integration_oid,
                    existing.payload_json,
                    existing.payload_digest,
                )
                if actual != expected:
                    raise ReviewJournalConflict(
                        "review operation ID conflicts with immutable event data"
                    )
                connection.commit()
                return existing

            previous = connection.execute(
                """
                SELECT event_seq, event_digest FROM review_events
                WHERE plan_id = ? ORDER BY event_seq DESC LIMIT 1
                """,
                (plan_id,),
            ).fetchone()
            event_seq = 1 if previous is None else int(previous["event_seq"]) + 1
            previous_digest = None if previous is None else str(previous["event_digest"])
            event_payload = {
                "event_seq": event_seq,
                "generation": generation,
                "integration_oid": integration_oid,
                "kind": kind,
                "operation_id": operation_id,
                "payload_digest": payload_digest,
                "plan_id": plan_id,
                "previous_event_digest": previous_digest,
                "target_digest": target_digest,
            }
            event_digest = _domain_digest(_EVENT_DOMAIN, _canonical_json(event_payload))
            connection.execute(
                """
                INSERT INTO review_events (
                    plan_id, event_seq, generation, operation_id, kind,
                    target_digest, integration_oid, payload_json, payload_digest,
                    previous_event_digest, event_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    event_seq,
                    generation,
                    operation_id,
                    kind,
                    target_digest,
                    integration_oid,
                    payload_json,
                    payload_digest,
                    previous_digest,
                    event_digest,
                ),
            )
            connection.commit()
            return ReviewJournalEvent(
                plan_id=plan_id,
                event_seq=event_seq,
                generation=generation,
                operation_id=operation_id,
                kind=kind,
                target_digest=target_digest,
                integration_oid=integration_oid,
                payload_json=payload_json,
                payload_digest=payload_digest,
                previous_event_digest=previous_digest,
                event_digest=event_digest,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def latest_pass(self, plan_id: str, target_digest: str) -> ReviewJournalEvent | None:
        plan_id = _require_text(plan_id, "plan_id", maximum=256)
        target_digest = _require_digest(target_digest, "target_digest")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM review_events
                WHERE plan_id = ? AND target_digest = ? AND kind = 'review_pass'
                ORDER BY event_seq DESC LIMIT 1
                """,
                (plan_id, target_digest),
            ).fetchone()
        return None if row is None else self._from_row(row)


@dataclass(frozen=True)
class ReviewJob:
    job_id: str
    source_kind: str
    source_id: str
    target_digest: str
    policy_digest: str
    integration_oid: str
    check_receipt_digest: str
    state: str
    current_generation: int | None
    owner_id: str | None
    fencing_token: int
    lease_expires_at_ns: int | None
    cancel_requested: bool
    adapter_version: str
    owner_session_id: str
    owner_profile: str
    workspace: str
    adapter_state_json: str = field(repr=False)
    runtime_routes_json: str = field(repr=False)
    next_manual_repair_attempt: int
    prepared_consumer_plan_id: str | None
    prepared_target_digest: str | None
    prepared_review_receipt_digest: str | None
    landing_owner_pid: int | None
    landing_owner_process_start_id: str | None
    landing_repository_effect_lock_path: str | None
    landing_authorization_digest: str | None
    landing_operation_active: bool


@dataclass(frozen=True)
class ReviewGeneration:
    job_id: str
    generation: int
    state: str
    target_digest: str
    integration_oid: str
    check_receipt_digest: str
    target_json: str = field(repr=False)
    artifact_json: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class StoredReviewerReceipt:
    job_id: str
    generation: int
    slot: str
    target_digest: str
    integration_oid: str
    output_digest: str
    verdict_digest: str
    passed: bool
    operation_id: str
    fencing_token: int
    receipt_json: str = field(repr=False)


@dataclass(frozen=True)
class StoredReviewBlocked:
    job_id: str
    generation: int
    target_digest: str
    integration_oid: str
    check_receipt_digest: str
    review_receipt_digest: str
    blocking_findings_json: str = field(repr=False)
    blocking_findings_digest: str
    operation_id: str
    fencing_token: int


@dataclass(frozen=True)
class StoredHostCheckFailure:
    job_id: str
    generation: int
    target_digest: str
    integration_oid: str
    check_failure_digest: str
    blocking_findings_json: str = field(repr=False)
    blocking_findings_digest: str
    operation_id: str
    fencing_token: int


@dataclass(frozen=True)
class StoredRepairCheckpoint:
    job_id: str
    prior_generation: int
    generation: int
    prior_target_digest: str
    integration_oid: str
    integration_tree_oid: str
    integration_ref: str
    integration_receipt_digest: str
    candidate_receipts_json: str = field(repr=False)
    candidate_receipts_digest: str
    operation_id: str
    fencing_token: int


@dataclass(frozen=True)
class StoredRepairCandidate:
    job_id: str
    prior_generation: int
    manifest_slice_id: str
    repair_attempt: int
    prior_target_digest: str
    base_integration_oid: str
    attempt_plan_id: str
    candidate_receipt_json: str = field(repr=False)
    changed_paths_json: str = field(repr=False)
    evidence_digest: str
    operation_id: str
    fencing_token: int


@dataclass(frozen=True)
class BestplanExecutionPipeline:
    """Immutable launch identity plus a monotonic isolated-attempt ordinal."""

    plan_id: str
    delegation_id: str
    job_id: str
    owner_session_id: str
    owner_profile: str
    workspace: str
    adapter_version: str
    adapter_state_json: str = field(repr=False)
    runtime_routes_json: str = field(repr=False)
    candidate_count: int
    state: str
    next_attempt_ordinal: int
    active_attempt_ordinal: int | None
    attempt_owner_pid: int | None
    attempt_owner_process_start_id: str | None
    cancel_requested: bool


@dataclass(frozen=True)
class StoredCheckCheckpoint:
    job_id: str
    generation: int
    target_digest: str
    integration_oid: str
    check_receipt_digest: str
    target_json: str = field(repr=False)
    check_receipt_json: str = field(repr=False)
    check_receipt_json_digest: str
    operation_id: str
    fencing_token: int


@dataclass(frozen=True)
class StoredManualCheckpoint:
    job_id: str
    prior_generation: int
    generation: int
    phase: str
    prior_target_digest: str
    snapshot_digest: str
    live_state_digest: str
    payload_json: str = field(repr=False)
    payload_digest: str
    operation_id: str
    fencing_token: int


@dataclass(frozen=True)
class StoredReviewPass:
    job_id: str
    generation: int
    target_digest: str
    integration_oid: str
    check_receipt_digest: str
    review_receipt_digest: str
    slot_receipts_json: str = field(repr=False)
    slot_receipts_digest: str
    operation_id: str
    fencing_token: int


@dataclass(frozen=True)
class ReviewPassConsumption:
    job_id: str
    generation: int
    consumer_plan_id: str
    target_digest: str
    review_receipt_digest: str
    fencing_token: int


@dataclass(frozen=True)
class ReviewStoreEvent:
    job_id: str
    event_seq: int
    generation: int
    owner_id: str
    fencing_token: int
    operation_id: str
    kind: str
    target_digest: str
    payload_json: str
    payload_digest: str
    previous_event_digest: str | None
    event_digest: str


@dataclass(frozen=True)
class ReviewResume:
    job_id: str
    generation: int
    target_digest: str
    adopted_reviewer_receipts: tuple[StoredReviewerReceipt, ...]
    missing_reviewer_slots: tuple[str, ...]
    next_action: str
    blocking_findings_json: str = "[]"
    review_receipt_digest: str | None = None
    repair_checkpoint: StoredRepairCheckpoint | None = None
    check_receipt_json: str | None = None
    review_pass: StoredReviewPass | None = None


class ReviewStore:
    """Durable review jobs with transactional leases and fencing tokens."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_jobs (
                    job_id TEXT PRIMARY KEY,
                    source_kind TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    target_digest TEXT NOT NULL,
                    policy_digest TEXT NOT NULL,
                    integration_oid TEXT NOT NULL,
                    check_receipt_digest TEXT NOT NULL,
                    state TEXT NOT NULL,
                    current_generation INTEGER,
                    owner_id TEXT,
                    fencing_token INTEGER NOT NULL,
                    lease_expires_at_ns INTEGER,
                    cancel_requested INTEGER NOT NULL,
                    adapter_version TEXT NOT NULL DEFAULT '',
                    owner_session_id TEXT NOT NULL DEFAULT '',
                    owner_profile TEXT NOT NULL DEFAULT '',
                    workspace TEXT NOT NULL DEFAULT '',
                    adapter_state_json TEXT NOT NULL DEFAULT '{}',
                    runtime_routes_json TEXT NOT NULL DEFAULT '[]',
                    next_manual_repair_attempt INTEGER NOT NULL DEFAULT 0,
                    prepared_consumer_plan_id TEXT,
                    prepared_target_digest TEXT,
                    prepared_review_receipt_digest TEXT,
                    landing_owner_pid INTEGER,
                    landing_owner_process_start_id TEXT,
                    landing_repository_effect_lock_path TEXT,
                    landing_authorization_digest TEXT,
                    landing_operation_active INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_generations (
                    job_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    target_digest TEXT NOT NULL,
                    integration_oid TEXT NOT NULL,
                    check_receipt_digest TEXT NOT NULL,
                    target_json TEXT NOT NULL,
                    artifact_json TEXT,
                    PRIMARY KEY (job_id, generation),
                    FOREIGN KEY (job_id) REFERENCES review_jobs(job_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_reviewer_receipts (
                    job_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    slot TEXT NOT NULL,
                    target_digest TEXT NOT NULL,
                    integration_oid TEXT NOT NULL,
                    output_digest TEXT NOT NULL,
                    verdict_digest TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    operation_id TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    receipt_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (job_id, generation, slot),
                    UNIQUE (job_id, operation_id),
                    FOREIGN KEY (job_id, generation)
                        REFERENCES review_generations(job_id, generation)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_pass_receipts (
                    job_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    target_digest TEXT NOT NULL,
                    integration_oid TEXT NOT NULL,
                    check_receipt_digest TEXT NOT NULL,
                    review_receipt_digest TEXT NOT NULL,
                    slot_receipts_json TEXT NOT NULL,
                    slot_receipts_digest TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    PRIMARY KEY (job_id, generation),
                    UNIQUE (review_receipt_digest),
                    UNIQUE (job_id, operation_id),
                    FOREIGN KEY (job_id, generation)
                        REFERENCES review_generations(job_id, generation)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_blocked_receipts (
                    job_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    target_digest TEXT NOT NULL,
                    integration_oid TEXT NOT NULL,
                    check_receipt_digest TEXT NOT NULL,
                    review_receipt_digest TEXT NOT NULL,
                    blocking_findings_json TEXT NOT NULL,
                    blocking_findings_digest TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    PRIMARY KEY (job_id, generation),
                    UNIQUE (job_id, operation_id),
                    FOREIGN KEY (job_id, generation)
                        REFERENCES review_generations(job_id, generation)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_host_check_failures (
                    job_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    target_digest TEXT NOT NULL,
                    integration_oid TEXT NOT NULL,
                    check_failure_digest TEXT NOT NULL,
                    blocking_findings_json TEXT NOT NULL,
                    blocking_findings_digest TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    PRIMARY KEY (job_id, generation),
                    UNIQUE (job_id, operation_id),
                    FOREIGN KEY (job_id, generation)
                        REFERENCES review_generations(job_id, generation)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_repair_candidates (
                    job_id TEXT NOT NULL,
                    prior_generation INTEGER NOT NULL,
                    manifest_slice_id TEXT NOT NULL,
                    repair_attempt INTEGER NOT NULL,
                    prior_target_digest TEXT NOT NULL,
                    base_integration_oid TEXT NOT NULL,
                    attempt_plan_id TEXT NOT NULL,
                    candidate_receipt_json TEXT NOT NULL,
                    changed_paths_json TEXT NOT NULL,
                    evidence_digest TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    PRIMARY KEY (
                        job_id, prior_generation, manifest_slice_id,
                        repair_attempt
                    ),
                    UNIQUE (job_id, operation_id),
                    UNIQUE (job_id, evidence_digest),
                    FOREIGN KEY (job_id) REFERENCES review_jobs(job_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bestplan_execution_pipelines (
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
                    next_attempt_ordinal INTEGER NOT NULL DEFAULT 0,
                    active_attempt_ordinal INTEGER,
                    attempt_owner_pid INTEGER,
                    attempt_owner_process_start_id TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_repair_checkpoints (
                    job_id TEXT NOT NULL,
                    prior_generation INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    prior_target_digest TEXT NOT NULL,
                    integration_oid TEXT NOT NULL,
                    integration_tree_oid TEXT NOT NULL,
                    integration_ref TEXT NOT NULL,
                    integration_receipt_digest TEXT NOT NULL,
                    candidate_receipts_json TEXT NOT NULL,
                    candidate_receipts_digest TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    PRIMARY KEY (job_id, generation),
                    UNIQUE (job_id, operation_id),
                    FOREIGN KEY (job_id) REFERENCES review_jobs(job_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_check_checkpoints (
                    job_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    target_digest TEXT NOT NULL,
                    integration_oid TEXT NOT NULL,
                    check_receipt_digest TEXT NOT NULL,
                    target_json TEXT NOT NULL,
                    check_receipt_json TEXT NOT NULL,
                    check_receipt_json_digest TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    PRIMARY KEY (job_id, generation),
                    UNIQUE (job_id, operation_id),
                    FOREIGN KEY (job_id, generation)
                        REFERENCES review_generations(job_id, generation)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_manual_checkpoints (
                    job_id TEXT NOT NULL,
                    prior_generation INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    phase TEXT NOT NULL,
                    prior_target_digest TEXT NOT NULL,
                    snapshot_digest TEXT NOT NULL,
                    live_state_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    PRIMARY KEY (job_id, generation, phase),
                    UNIQUE (job_id, operation_id),
                    FOREIGN KEY (job_id) REFERENCES review_jobs(job_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_pass_consumptions (
                    job_id TEXT PRIMARY KEY,
                    generation INTEGER NOT NULL,
                    consumer_plan_id TEXT NOT NULL UNIQUE,
                    target_digest TEXT NOT NULL,
                    review_receipt_digest TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    FOREIGN KEY (job_id, generation)
                        REFERENCES review_pass_receipts(job_id, generation)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_store_events (
                    job_id TEXT NOT NULL,
                    event_seq INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    owner_id TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    operation_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    target_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    previous_event_digest TEXT,
                    event_digest TEXT NOT NULL,
                    PRIMARY KEY (job_id, event_seq),
                    UNIQUE (job_id, operation_id),
                    UNIQUE (event_digest),
                    FOREIGN KEY (job_id) REFERENCES review_jobs(job_id)
                )
                """
            )
            job_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(review_jobs)")
            }
            for name, sql_type in (
                ("prepared_consumer_plan_id", "TEXT"),
                ("prepared_target_digest", "TEXT"),
                ("prepared_review_receipt_digest", "TEXT"),
                ("adapter_version", "TEXT NOT NULL DEFAULT ''"),
                ("owner_session_id", "TEXT NOT NULL DEFAULT ''"),
                ("owner_profile", "TEXT NOT NULL DEFAULT ''"),
                ("workspace", "TEXT NOT NULL DEFAULT ''"),
                ("adapter_state_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("runtime_routes_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("next_manual_repair_attempt", "INTEGER NOT NULL DEFAULT 0"),
                ("landing_owner_pid", "INTEGER"),
                ("landing_owner_process_start_id", "TEXT"),
                ("landing_repository_effect_lock_path", "TEXT"),
                ("landing_authorization_digest", "TEXT"),
                ("landing_operation_active", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in job_columns:
                    connection.execute(
                        f"ALTER TABLE review_jobs ADD COLUMN {name} {sql_type}"
                    )
            pipeline_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(bestplan_execution_pipelines)"
                )
            }
            for name, sql_type in (
                ("active_attempt_ordinal", "INTEGER"),
                ("attempt_owner_pid", "INTEGER"),
                ("attempt_owner_process_start_id", "TEXT"),
                ("cancel_requested", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in pipeline_columns:
                    connection.execute(
                        "ALTER TABLE bestplan_execution_pipelines "
                        f"ADD COLUMN {name} {sql_type}"
                    )
            receipt_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(review_reviewer_receipts)"
                )
            }
            generation_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(review_generations)"
                )
            }
            if "artifact_json" not in generation_columns:
                connection.execute(
                    "ALTER TABLE review_generations ADD COLUMN artifact_json TEXT"
                )
            if "passed" not in receipt_columns:
                connection.execute(
                    "ALTER TABLE review_reviewer_receipts "
                    "ADD COLUMN passed INTEGER NOT NULL DEFAULT 0"
                )
            if "receipt_json" not in receipt_columns:
                connection.execute(
                    "ALTER TABLE review_reviewer_receipts "
                    "ADD COLUMN receipt_json TEXT NOT NULL DEFAULT '{}'"
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_review_receipts_target
                ON review_reviewer_receipts (
                    job_id, generation, target_digest, integration_oid, slot
                )
                """
            )
            connection.execute(
                "DROP TRIGGER IF EXISTS trg_review_generation_identity_immutable"
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_review_generation_identity_immutable
                BEFORE UPDATE OF target_digest, integration_oid,
                    check_receipt_digest, target_json, artifact_json
                    ON review_generations
                BEGIN
                    SELECT RAISE(ABORT, 'review generation identity is immutable');
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_review_generation_delete_immutable
                BEFORE DELETE ON review_generations
                BEGIN
                    SELECT RAISE(ABORT, 'review generation is immutable');
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_review_receipt_update_immutable
                BEFORE UPDATE ON review_reviewer_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'review receipt is immutable');
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_review_receipt_delete_immutable
                BEFORE DELETE ON review_reviewer_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'review receipt is immutable');
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_review_pass_update_immutable
                BEFORE UPDATE ON review_pass_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'review pass receipt is immutable');
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_review_pass_delete_immutable
                BEFORE DELETE ON review_pass_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'review pass receipt is immutable');
                END
                """
            )
            for table, label in (
                ("review_blocked_receipts", "review blocked receipt"),
                ("review_host_check_failures", "review host check failure"),
                ("review_repair_candidates", "review repair candidate"),
                ("review_repair_checkpoints", "review repair checkpoint"),
                ("review_check_checkpoints", "review check checkpoint"),
                ("review_manual_checkpoints", "manual review checkpoint"),
            ):
                connection.execute(
                    f"""
                    CREATE TRIGGER IF NOT EXISTS trg_{table}_update_immutable
                    BEFORE UPDATE ON {table}
                    BEGIN
                        SELECT RAISE(ABORT, '{label} is immutable');
                    END
                    """
                )
                connection.execute(
                    f"""
                    CREATE TRIGGER IF NOT EXISTS trg_{table}_delete_immutable
                    BEFORE DELETE ON {table}
                    BEGIN
                        SELECT RAISE(ABORT, '{label} is immutable');
                    END
                    """
                )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_bestplan_execution_pipeline_identity_immutable
                BEFORE UPDATE OF delegation_id, job_id, owner_session_id,
                    owner_profile, workspace, adapter_version,
                    adapter_state_json, runtime_routes_json, candidate_count
                    ON bestplan_execution_pipelines
                BEGIN
                    SELECT RAISE(ABORT, 'execution pipeline identity is immutable');
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_review_consumption_update_immutable
                BEFORE UPDATE ON review_pass_consumptions
                BEGIN
                    SELECT RAISE(ABORT, 'review pass consumption is immutable');
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_review_consumption_delete_immutable
                BEFORE DELETE ON review_pass_consumptions
                BEGIN
                    SELECT RAISE(ABORT, 'review pass consumption is immutable');
                END
                """
            )
            connection.execute(
                "DROP TRIGGER IF EXISTS trg_review_generation_frozen_after_prepare"
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_review_generation_frozen_after_prepare
                BEFORE INSERT ON review_generations
                WHEN EXISTS (
                    SELECT 1 FROM review_jobs
                    WHERE job_id=NEW.job_id
                      AND prepared_consumer_plan_id IS NOT NULL
                )
                BEGIN
                    SELECT RAISE(ABORT, 'review job is frozen after landing prepare');
                END
                """
            )
            connection.execute(
                "DROP TRIGGER IF EXISTS trg_review_job_identity_frozen_after_prepare"
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_review_job_adapter_identity_immutable
                BEFORE UPDATE OF adapter_version, owner_session_id, owner_profile,
                    workspace, adapter_state_json, runtime_routes_json
                    ON review_jobs
                BEGIN
                    SELECT RAISE(ABORT, 'review adapter identity is immutable');
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_review_job_identity_frozen_after_prepare
                BEFORE UPDATE ON review_jobs
                WHEN OLD.prepared_consumer_plan_id IS NOT NULL AND (
                    NEW.current_generation IS NOT OLD.current_generation
                    OR NEW.target_digest IS NOT OLD.target_digest
                    OR NEW.integration_oid IS NOT OLD.integration_oid
                    OR NEW.check_receipt_digest IS NOT OLD.check_receipt_digest
                    OR NEW.prepared_consumer_plan_id IS NOT OLD.prepared_consumer_plan_id
                    OR NEW.prepared_target_digest IS NOT OLD.prepared_target_digest
                    OR NEW.prepared_review_receipt_digest
                        IS NOT OLD.prepared_review_receipt_digest
                )
                BEGIN
                    SELECT RAISE(ABORT, 'review job identity is frozen after landing prepare');
                END
                """
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _lease_now_ns(self) -> int:
        """Return the injectable wall clock used for lease authorization."""

        return time.time_ns()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _write(self, callback: Callable[[sqlite3.Connection], object]) -> object:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = callback(connection)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> ReviewJob:
        values = dict(row)
        values["cancel_requested"] = bool(values["cancel_requested"])
        values["landing_operation_active"] = bool(
            values["landing_operation_active"]
        )
        return ReviewJob(**values)

    @staticmethod
    def _generation_from_row(row: sqlite3.Row) -> ReviewGeneration:
        return ReviewGeneration(**dict(row))

    @staticmethod
    def _receipt_from_row(row: sqlite3.Row) -> StoredReviewerReceipt:
        values = dict(row)
        values["passed"] = bool(values["passed"])
        return StoredReviewerReceipt(**values)

    @staticmethod
    def _blocked_from_row(row: sqlite3.Row) -> StoredReviewBlocked:
        return StoredReviewBlocked(**dict(row))

    @staticmethod
    def _host_check_failure_from_row(
        row: sqlite3.Row,
    ) -> StoredHostCheckFailure:
        return StoredHostCheckFailure(**dict(row))

    @staticmethod
    def _repair_from_row(row: sqlite3.Row) -> StoredRepairCheckpoint:
        return StoredRepairCheckpoint(**dict(row))

    @staticmethod
    def _repair_candidate_from_row(
        row: sqlite3.Row,
    ) -> StoredRepairCandidate:
        return StoredRepairCandidate(**dict(row))

    @staticmethod
    def _execution_pipeline_from_row(
        row: sqlite3.Row,
    ) -> BestplanExecutionPipeline:
        values = dict(row)
        return BestplanExecutionPipeline(
            plan_id=values["plan_id"],
            delegation_id=values["delegation_id"],
            job_id=values["job_id"],
            owner_session_id=values["owner_session_id"],
            owner_profile=values["owner_profile"],
            workspace=values["workspace"],
            adapter_version=values["adapter_version"],
            adapter_state_json=values["adapter_state_json"],
            runtime_routes_json=values["runtime_routes_json"],
            candidate_count=int(values["candidate_count"]),
            state=values["state"],
            next_attempt_ordinal=int(values["next_attempt_ordinal"]),
            active_attempt_ordinal=(
                None
                if values.get("active_attempt_ordinal") is None
                else int(values["active_attempt_ordinal"])
            ),
            attempt_owner_pid=(
                None
                if values.get("attempt_owner_pid") is None
                else int(values["attempt_owner_pid"])
            ),
            attempt_owner_process_start_id=values.get(
                "attempt_owner_process_start_id"
            ),
            cancel_requested=bool(values.get("cancel_requested", 0)),
        )

    @staticmethod
    def _check_from_row(row: sqlite3.Row) -> StoredCheckCheckpoint:
        return StoredCheckCheckpoint(**dict(row))

    @staticmethod
    def _manual_checkpoint_from_row(
        row: sqlite3.Row,
    ) -> StoredManualCheckpoint:
        return StoredManualCheckpoint(**dict(row))

    @staticmethod
    def _pass_from_row(row: sqlite3.Row) -> StoredReviewPass:
        return StoredReviewPass(**dict(row))

    @staticmethod
    def _consumption_from_row(row: sqlite3.Row) -> ReviewPassConsumption:
        return ReviewPassConsumption(**dict(row))

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> ReviewStoreEvent:
        return ReviewStoreEvent(**dict(row))

    @staticmethod
    def _job_row(connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM review_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise ReviewValidationError("review job does not exist")
        return row

    def _require_lease(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        owner_id: str,
        fencing_token: int,
        allow_cancel_requested: bool = False,
        allow_expired: bool = False,
    ) -> sqlite3.Row:
        row = self._job_row(connection, job_id)
        if (
            row["owner_id"] != owner_id
            or int(row["fencing_token"]) != fencing_token
            or row["lease_expires_at_ns"] is None
        ):
            raise ReviewLeaseConflict("review mutation has a stale owner lease")
        if (
            not allow_expired
            and self._lease_now_ns() > int(row["lease_expires_at_ns"])
        ):
            raise ReviewLeaseConflict("review owner lease has expired")
        if row["state"] == "landing_claimed":
            raise ReviewLeaseConflict("review job is frozen after landing prepare")
        if bool(row["cancel_requested"]) and not allow_cancel_requested:
            raise ReviewLeaseConflict("review job cancellation is already requested")
        return row

    def create_job(
        self,
        *,
        job_id: str,
        source_kind: str,
        source_id: str,
        target_digest: str,
        policy_digest: str,
        integration_oid: str,
        check_receipt_digest: str,
        adapter_version: str = "",
        owner_session_id: str = "",
        owner_profile: str = "",
        workspace: str = "",
        adapter_state: object | None = None,
        runtime_routes: object | None = None,
        initial_target: ReviewTarget | None = None,
        initial_artifact: ReviewArtifact | None = None,
    ) -> ReviewJob:
        job_id = _require_text(job_id, "job_id", maximum=256)
        source_kind = _require_text(source_kind, "source_kind", maximum=64)
        if source_kind not in {"bestplan_integration", "manual_snapshot"}:
            raise ReviewValidationError("review job source kind is unsupported")
        source_id = _require_text(source_id, "source_id", maximum=512)
        target_digest = _require_digest(target_digest, "target_digest")
        policy_digest = _require_digest(policy_digest, "policy_digest")
        integration_oid = _require_oid(integration_oid, "integration_oid")
        check_receipt_digest = _require_digest(
            check_receipt_digest, "check_receipt_digest"
        )
        if adapter_version:
            adapter_version = _require_text(
                adapter_version, "adapter_version", maximum=128
            )
        if owner_session_id:
            owner_session_id = _require_text(
                owner_session_id, "owner_session_id", maximum=512
            )
        if owner_profile:
            owner_profile = _require_text(
                owner_profile, "owner_profile", maximum=256
            )
        if workspace:
            workspace = _require_text(workspace, "workspace", maximum=4096)
            resolved_workspace = str(Path(workspace).expanduser().resolve())
            if not Path(workspace).is_absolute() or workspace != resolved_workspace:
                raise ReviewValidationError("workspace must be a canonical path")
        adapter_state_json = _recovery_metadata_json(
            {} if adapter_state is None else adapter_state,
            "adapter_state",
            execution_protocol=1,
        )
        runtime_routes_json = _recovery_metadata_json(
            [] if runtime_routes is None else runtime_routes,
            "runtime_routes",
            execution_protocol=2,
        )
        if (initial_target is None) != (initial_artifact is None):
            raise ReviewValidationError(
                "initial review generation evidence is incomplete"
            )
        initial_artifact_json: str | None = None
        if initial_target is not None:
            if (
                source_kind != "manual_snapshot"
                or not isinstance(initial_target, ReviewTarget)
                or not isinstance(initial_artifact, ReviewArtifact)
                or initial_target.generation != 0
                or initial_target.source_kind != source_kind
                or initial_target.plan_id != source_id
                or initial_target.target_digest != target_digest
                or initial_target.policy_digest != policy_digest
                or initial_target.integration_oid != integration_oid
                or initial_target.check_receipt_digest != check_receipt_digest
                or initial_artifact.target_digest != target_digest
            ):
                raise ReviewValidationError(
                    "initial manual review generation differs from its job"
                )
            initial_artifact_json = initial_artifact.canonical_json

        def write(connection: sqlite3.Connection) -> ReviewJob:
            existing = connection.execute(
                "SELECT * FROM review_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            immutable = (
                source_kind,
                source_id,
                target_digest,
                policy_digest,
                integration_oid,
                check_receipt_digest,
                adapter_version,
                owner_session_id,
                owner_profile,
                workspace,
                adapter_state_json,
                runtime_routes_json,
            )
            if existing is not None:
                current = tuple(
                    existing[name]
                    for name in (
                        "source_kind",
                        "source_id",
                        "target_digest",
                        "policy_digest",
                        "integration_oid",
                        "check_receipt_digest",
                        "adapter_version",
                        "owner_session_id",
                        "owner_profile",
                        "workspace",
                        "adapter_state_json",
                        "runtime_routes_json",
                    )
                )
                if current != immutable:
                    raise ReviewStoreConflict(
                        "review job ID conflicts with immutable target data"
                    )
            else:
                pipeline = connection.execute(
                    "SELECT state, cancel_requested FROM "
                    "bestplan_execution_pipelines WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                if pipeline is not None and (
                    pipeline["state"] != "pending"
                    or bool(pipeline["cancel_requested"])
                ):
                    raise ReviewStoreConflict(
                        "execution pipeline cannot start review after cancellation"
                    )
                connection.execute(
                    """
                    INSERT INTO review_jobs (
                        job_id, source_kind, source_id, target_digest, policy_digest,
                        integration_oid, check_receipt_digest, state,
                        current_generation, owner_id, fencing_token,
                        lease_expires_at_ns, cancel_requested, adapter_version,
                        owner_session_id, owner_profile, workspace,
                        adapter_state_json, runtime_routes_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', NULL, NULL, 0, NULL, 0,
                              ?, ?, ?, ?, ?, ?)
                    """,
                    (job_id, *immutable),
                )
                existing = self._job_row(connection, job_id)
            if initial_target is not None:
                generation_row = connection.execute(
                    """
                    SELECT * FROM review_generations
                    WHERE job_id=? AND generation=0
                    """,
                    (job_id,),
                ).fetchone()
                generation_identity = (
                    initial_target.target_digest,
                    initial_target.integration_oid,
                    initial_target.check_receipt_digest,
                    initial_target.canonical_json,
                    initial_artifact_json,
                )
                if generation_row is None:
                    if (
                        existing["current_generation"] is not None
                        or existing["owner_id"] is not None
                        or existing["state"] != "queued"
                        or int(existing["fencing_token"]) != 0
                    ):
                        raise ReviewStoreConflict(
                            "initial manual generation checkpoint is missing"
                        )
                    connection.execute(
                        """
                        INSERT INTO review_generations (
                            job_id, generation, state, target_digest,
                            integration_oid, check_receipt_digest, target_json,
                            artifact_json
                        ) VALUES (?, 0, 'reviewing', ?, ?, ?, ?, ?)
                        """,
                        (job_id, *generation_identity),
                    )
                    if connection.execute(
                        """
                        UPDATE review_jobs
                        SET state='reviewing', current_generation=0,
                            fencing_token=1
                        WHERE job_id=? AND state='queued'
                          AND current_generation IS NULL AND owner_id IS NULL
                          AND fencing_token=0
                        """,
                        (job_id,),
                    ).rowcount != 1:
                        raise ReviewStoreConflict(
                            "initial manual generation lost its queued job"
                        )
                    self._append_event_conn(
                        connection,
                        job_id=job_id,
                        generation=0,
                        owner_id="manual-bootstrap",
                        fencing_token=1,
                        operation_id="manual:0:generation_started:bootstrap",
                        kind="generation_started",
                        target_digest=initial_target.target_digest,
                        payload={
                            "target": json.loads(initial_target.canonical_json)
                        },
                    )
                else:
                    stored_identity = tuple(
                        generation_row[name]
                        for name in (
                            "target_digest",
                            "integration_oid",
                            "check_receipt_digest",
                            "target_json",
                            "artifact_json",
                        )
                    )
                    if stored_identity != generation_identity:
                        raise ReviewStoreConflict(
                            "initial manual generation identity conflicts"
                        )
            return self._job_from_row(self._job_row(connection, job_id))

        return self._write(write)  # type: ignore[return-value]

    def create_execution_pipeline(
        self,
        *,
        plan_id: str,
        delegation_id: str,
        job_id: str,
        owner_session_id: str,
        owner_profile: str,
        workspace: str,
        adapter_state: object,
        runtime_routes: object,
        candidate_count: int,
    ) -> BestplanExecutionPipeline:
        """Persist sanitized restart intent before candidate dispatch starts."""

        plan_id = _require_text(plan_id, "plan_id", maximum=256)
        delegation_id = _require_text(
            delegation_id, "delegation_id", maximum=256
        )
        job_id = _require_text(job_id, "job_id", maximum=256)
        owner_session_id = _require_text(
            owner_session_id, "owner_session_id", maximum=512
        )
        if owner_profile:
            owner_profile = _require_text(
                owner_profile, "owner_profile", maximum=256
            )
        else:
            owner_profile = ""
        workspace = _require_text(workspace, "workspace", maximum=4096)
        resolved_workspace = str(Path(workspace).expanduser().resolve())
        if not Path(workspace).is_absolute() or workspace != resolved_workspace:
            raise ReviewValidationError("workspace must be a canonical path")
        candidate_count = _require_positive_int(
            candidate_count, "candidate_count"
        )
        adapter_version = "local-bestplan-execution.v1"
        adapter_state_json = _recovery_metadata_json(
            adapter_state, "adapter_state", execution_protocol=1,
        )
        runtime_routes_json = _recovery_metadata_json(
            runtime_routes, "runtime_routes", execution_protocol=2,
        )
        immutable = (
            delegation_id,
            job_id,
            owner_session_id,
            owner_profile,
            workspace,
            adapter_version,
            adapter_state_json,
            runtime_routes_json,
            candidate_count,
        )

        def write(connection: sqlite3.Connection) -> BestplanExecutionPipeline:
            existing = connection.execute(
                "SELECT * FROM bestplan_execution_pipelines WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            if existing is not None:
                current = tuple(
                    existing[name]
                    for name in (
                        "delegation_id", "job_id", "owner_session_id",
                        "owner_profile", "workspace", "adapter_version",
                        "adapter_state_json", "runtime_routes_json",
                        "candidate_count",
                    )
                )
                if current != immutable:
                    raise ReviewStoreConflict(
                        "execution pipeline conflicts with immutable identity"
                    )
                return self._execution_pipeline_from_row(existing)
            connection.execute(
                """
                INSERT INTO bestplan_execution_pipelines (
                    plan_id, delegation_id, job_id, owner_session_id,
                    owner_profile, workspace, adapter_version,
                    adapter_state_json, runtime_routes_json, candidate_count,
                    state, next_attempt_ordinal
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0)
                """,
                (plan_id, *immutable),
            )
            stored = connection.execute(
                "SELECT * FROM bestplan_execution_pipelines WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            if stored is None:
                raise ReviewStoreConflict("execution pipeline was not stored")
            return self._execution_pipeline_from_row(stored)

        return self._write(write)  # type: ignore[return-value]

    def get_execution_pipeline(
        self, plan_id: str,
    ) -> BestplanExecutionPipeline:
        plan_id = _require_text(plan_id, "plan_id", maximum=256)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM bestplan_execution_pipelines WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
        if row is None:
            raise ReviewValidationError("execution pipeline does not exist")
        return self._execution_pipeline_from_row(row)

    def request_execution_pipeline_cancel(
        self,
        *,
        plan_id: str,
        delegation_id: str,
        job_id: str,
    ) -> bool:
        """Cancel pre-review execution before signalling its live children.

        Return ``False`` when the exact review job won the transaction. The
        caller must then cancel that durable review job instead.
        """

        plan_id = _require_text(plan_id, "plan_id", maximum=256)
        delegation_id = _require_text(
            delegation_id, "delegation_id", maximum=256
        )
        job_id = _require_text(job_id, "job_id", maximum=256)

        def write(connection: sqlite3.Connection) -> bool:
            row = connection.execute(
                "SELECT * FROM bestplan_execution_pipelines WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            if row is None:
                raise ReviewValidationError("execution pipeline does not exist")
            if (
                row["delegation_id"] != delegation_id
                or row["job_id"] != job_id
            ):
                raise ReviewStoreConflict(
                    "execution pipeline cancellation identity differs"
                )
            if connection.execute(
                "SELECT 1 FROM review_jobs WHERE job_id=?", (job_id,),
            ).fetchone() is not None:
                return False
            if row["state"] in {"cancel_requested", "cancelled"} and bool(
                row["cancel_requested"]
            ):
                return True
            if row["state"] != "pending":
                raise ReviewStoreConflict(
                    "execution pipeline is not cancellable"
                )
            changed = connection.execute(
                "UPDATE bestplan_execution_pipelines "
                "SET state='cancel_requested', cancel_requested=1 "
                "WHERE plan_id=? AND delegation_id=? AND job_id=? "
                "AND state='pending' AND cancel_requested=0",
                (plan_id, delegation_id, job_id),
            ).rowcount
            if changed != 1:
                raise ReviewStoreConflict(
                    "execution pipeline cancellation lost its fence"
                )
            return True

        return bool(self._write(write))

    def finalize_execution_pipeline_cancel(
        self,
        *,
        plan_id: str,
        delegation_id: str,
        job_id: str,
    ) -> BestplanExecutionPipeline:
        """Record pre-review child extinction after durable cancellation."""

        plan_id = _require_text(plan_id, "plan_id", maximum=256)
        delegation_id = _require_text(
            delegation_id, "delegation_id", maximum=256
        )
        job_id = _require_text(job_id, "job_id", maximum=256)

        def write(connection: sqlite3.Connection) -> BestplanExecutionPipeline:
            row = connection.execute(
                "SELECT * FROM bestplan_execution_pipelines WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            if row is None:
                raise ReviewValidationError("execution pipeline does not exist")
            if (
                row["delegation_id"] != delegation_id
                or row["job_id"] != job_id
            ):
                raise ReviewStoreConflict(
                    "execution pipeline cancellation identity differs"
                )
            if row["state"] == "cancelled" and bool(row["cancel_requested"]):
                return self._execution_pipeline_from_row(row)
            if row["state"] != "cancel_requested" or not bool(
                row["cancel_requested"]
            ):
                raise ReviewStoreConflict(
                    "execution pipeline cancellation was not requested"
                )
            if connection.execute(
                "UPDATE bestplan_execution_pipelines SET state='cancelled' "
                "WHERE plan_id=? AND state='cancel_requested' "
                "AND cancel_requested=1",
                (plan_id,),
            ).rowcount != 1:
                raise ReviewStoreConflict(
                    "execution pipeline cancel finalization lost its fence"
                )
            stored = connection.execute(
                "SELECT * FROM bestplan_execution_pipelines WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            return self._execution_pipeline_from_row(stored)

        return self._write(write)  # type: ignore[return-value]

    def allocate_execution_attempt(
        self,
        plan_id: str,
        *,
        owner_pid: int | None = None,
        owner_process_start_id: str | None = None,
        expected_owner_pid: int | None = None,
        expected_owner_process_start_id: str | None = None,
    ) -> int:
        """Allocate one fresh isolated attempt without reusing prior roots."""

        plan_id = _require_text(plan_id, "plan_id", maximum=256)
        if owner_pid is None:
            owner_pid = os.getpid()
        if isinstance(owner_pid, bool) or not isinstance(owner_pid, int) or owner_pid < 1:
            raise ReviewValidationError("execution attempt owner PID is invalid")
        owner_process_start_id = _require_text(
            owner_process_start_id,
            "execution attempt owner process identity",
            maximum=256,
        )
        if (expected_owner_pid is None) != (
            expected_owner_process_start_id is None
        ):
            raise ReviewValidationError(
                "execution attempt expected owner identity is partial"
            )
        if expected_owner_pid is not None and (
            isinstance(expected_owner_pid, bool)
            or not isinstance(expected_owner_pid, int)
            or expected_owner_pid < 1
        ):
            raise ReviewValidationError(
                "execution attempt expected owner PID is invalid"
            )
        if expected_owner_process_start_id is not None:
            expected_owner_process_start_id = _require_text(
                expected_owner_process_start_id,
                "execution attempt expected owner process identity",
                maximum=256,
            )

        def write(connection: sqlite3.Connection) -> int:
            row = connection.execute(
                "SELECT * FROM bestplan_execution_pipelines WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            if row is None:
                raise ReviewValidationError("execution pipeline does not exist")
            if bool(row["cancel_requested"]) or row["state"] in {
                "cancel_requested", "cancelled",
            }:
                raise ReviewStoreConflict("execution pipeline is cancelled")
            if row["state"] != "pending":
                raise ReviewStoreConflict("execution pipeline is not pending")
            current_owner = row["attempt_owner_pid"]
            current_start = row["attempt_owner_process_start_id"]
            if current_owner is not None:
                if (
                    expected_owner_pid != int(current_owner)
                    or expected_owner_process_start_id != current_start
                ):
                    raise ReviewStoreConflict(
                        "execution pipeline already has an active attempt owner"
                    )
            elif expected_owner_pid is not None:
                raise ReviewStoreConflict(
                    "execution pipeline expected owner differs"
                )
            ordinal = int(row["next_attempt_ordinal"])
            changed = connection.execute(
                """
                UPDATE bestplan_execution_pipelines
                SET next_attempt_ordinal=next_attempt_ordinal+1,
                    active_attempt_ordinal=?, attempt_owner_pid=?,
                    attempt_owner_process_start_id=?
                WHERE plan_id=? AND next_attempt_ordinal=?
                  AND attempt_owner_pid IS ?
                  AND attempt_owner_process_start_id IS ?
                """,
                (
                    ordinal, owner_pid, owner_process_start_id,
                    plan_id, ordinal, current_owner, current_start,
                ),
            ).rowcount
            if changed != 1:
                raise ReviewStoreConflict(
                    "execution attempt allocation lost its fence"
                )
            return ordinal

        return self._write(write)  # type: ignore[return-value]

    def mark_execution_pipeline_review_started(
        self, plan_id: str,
    ) -> BestplanExecutionPipeline:
        """Stop pre-review replay once the durable review job exists."""

        plan_id = _require_text(plan_id, "plan_id", maximum=256)

        def write(connection: sqlite3.Connection) -> BestplanExecutionPipeline:
            row = connection.execute(
                "SELECT * FROM bestplan_execution_pipelines WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            if row is None:
                raise ReviewValidationError("execution pipeline does not exist")
            if row["state"] == "pending":
                connection.execute(
                    "UPDATE bestplan_execution_pipelines SET state='review' "
                    "WHERE plan_id=? AND state='pending'",
                    (plan_id,),
                )
            stored = connection.execute(
                "SELECT * FROM bestplan_execution_pipelines WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            return self._execution_pipeline_from_row(stored)

        return self._write(write)  # type: ignore[return-value]

    def release_execution_attempt(
        self,
        plan_id: str,
        *,
        owner_pid: int,
        owner_process_start_id: str,
    ) -> BestplanExecutionPipeline:
        """Release one extinct pre-review attempt so recovery can take over."""

        plan_id = _require_text(plan_id, "plan_id", maximum=256)
        owner_process_start_id = _require_text(
            owner_process_start_id,
            "execution attempt owner process identity",
            maximum=256,
        )

        def write(connection: sqlite3.Connection) -> BestplanExecutionPipeline:
            row = connection.execute(
                "SELECT * FROM bestplan_execution_pipelines WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            if row is None:
                raise ReviewValidationError("execution pipeline does not exist")
            if row["state"] != "pending":
                return self._execution_pipeline_from_row(row)
            changed = connection.execute(
                """
                UPDATE bestplan_execution_pipelines
                SET active_attempt_ordinal=NULL, attempt_owner_pid=NULL,
                    attempt_owner_process_start_id=NULL
                WHERE plan_id=? AND state='pending'
                  AND attempt_owner_pid=?
                  AND attempt_owner_process_start_id=?
                """,
                (plan_id, owner_pid, owner_process_start_id),
            ).rowcount
            if changed != 1:
                raise ReviewStoreConflict(
                    "execution attempt release lost its owner fence"
                )
            stored = connection.execute(
                "SELECT * FROM bestplan_execution_pipelines WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            return self._execution_pipeline_from_row(stored)

        return self._write(write)  # type: ignore[return-value]

    def resolve_initial_check_pending(
        self,
        *,
        job_id: str,
        target: ReviewTarget,
        artifact: ReviewArtifact | None = None,
        check_receipt_json: str,
        owner_id: str,
        fencing_token: int,
        operation_id: str,
    ) -> ReviewGeneration:
        """Store the passed initial check without changing adapter identity."""

        job_id = _require_text(job_id, "job_id", maximum=256)
        if not isinstance(target, ReviewTarget) or target.generation != 0:
            raise ReviewValidationError("initial check target is invalid")
        if artifact is not None and (
            not isinstance(artifact, ReviewArtifact)
            or artifact.target_digest != target.target_digest
        ):
            raise ReviewValidationError("initial check artifact is invalid")
        check_receipt_json = _bounded_canonical_json(
            check_receipt_json,
            "check_receipt_json",
            expected_type=dict,
        )
        check_receipt_json_digest = hashlib.sha256(
            b"hermes.bestplan.check-checkpoint.v1\0"
            + check_receipt_json.encode("utf-8")
        ).hexdigest()
        owner_id = _require_text(owner_id, "owner_id", maximum=256)
        fencing_token = _require_nonnegative_int(fencing_token, "fencing_token")
        operation_id = _require_text(operation_id, "operation_id", maximum=256)

        def write(connection: sqlite3.Connection) -> ReviewGeneration:
            job = self._require_lease(
                connection,
                job_id=job_id,
                owner_id=owner_id,
                fencing_token=fencing_token,
            )
            try:
                prior_state = json.loads(str(job["adapter_state_json"]))
            except (json.JSONDecodeError, TypeError):
                prior_state = None
            if (
                job["state"] != "queued"
                or job["current_generation"] is not None
                or not isinstance(prior_state, dict)
                or not isinstance(prior_state.get("initial_check_pending"), dict)
                or job["source_kind"] != target.source_kind
                or job["source_id"] != target.plan_id
                or job["policy_digest"] != target.policy_digest
            ):
                raise ReviewStoreConflict("initial check checkpoint is stale")
            connection.execute(
                """
                INSERT INTO review_generations (
                    job_id, generation, state, target_digest, integration_oid,
                    check_receipt_digest, target_json, artifact_json
                ) VALUES (?, 0, 'reviewing', ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    target.target_digest,
                    target.integration_oid,
                    target.check_receipt_digest,
                    target.canonical_json,
                    None if artifact is None else artifact.canonical_json,
                ),
            )
            connection.execute(
                """
                INSERT INTO review_check_checkpoints (
                    job_id, generation, target_digest, integration_oid,
                    check_receipt_digest, target_json, check_receipt_json,
                    check_receipt_json_digest, operation_id, fencing_token
                ) VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    target.target_digest,
                    target.integration_oid,
                    target.check_receipt_digest,
                    target.canonical_json,
                    check_receipt_json,
                    check_receipt_json_digest,
                    operation_id,
                    fencing_token,
                ),
            )
            if connection.execute(
                """
                UPDATE review_jobs
                SET state='reviewing', current_generation=0,
                    target_digest=?, integration_oid=?, check_receipt_digest=?
                WHERE job_id=? AND state='queued' AND current_generation IS NULL
                  AND owner_id=? AND fencing_token=? AND cancel_requested=0
                """,
                (
                    target.target_digest,
                    target.integration_oid,
                    target.check_receipt_digest,
                    job_id,
                    owner_id,
                    fencing_token,
                ),
            ).rowcount != 1:
                raise ReviewLeaseConflict("initial check lost its owner lease")
            self._append_event_conn(
                connection,
                job_id=job_id,
                generation=0,
                owner_id=owner_id,
                fencing_token=fencing_token,
                operation_id=operation_id,
                kind="initial_checks_passed",
                target_digest=target.target_digest,
                payload={
                    "check_receipt_digest": target.check_receipt_digest,
                    "integration_oid": target.integration_oid,
                },
            )
            row = connection.execute(
                "SELECT * FROM review_generations WHERE job_id=? AND generation=0",
                (job_id,),
            ).fetchone()
            if row is None:
                raise ReviewStoreConflict("initial check checkpoint was not stored")
            return self._generation_from_row(row)

        return self._write(write)  # type: ignore[return-value]

    def record_initial_check_failure(
        self,
        *,
        job_id: str,
        target: ReviewTarget,
        check_failure_digest: str,
        blocking_findings_json: str,
        owner_id: str,
        fencing_token: int,
        operation_id: str,
    ) -> StoredHostCheckFailure:
        """Replace a transient initial-check checkpoint with exact failure."""

        job_id = _require_text(job_id, "job_id", maximum=256)
        if not isinstance(target, ReviewTarget) or target.generation != 0:
            raise ReviewValidationError("initial check failure target is invalid")
        check_failure_digest = _require_digest(
            check_failure_digest, "check_failure_digest"
        )
        if target.check_receipt_digest != check_failure_digest:
            raise ReviewValidationError("initial check failure digest differs")
        blocking_findings_json = _bounded_canonical_json(
            blocking_findings_json,
            "blocking_findings_json",
            expected_type=list,
        )
        if not json.loads(blocking_findings_json):
            raise ReviewValidationError("check failure findings must not be empty")
        blocking_findings_digest = hashlib.sha256(
            b"hermes.bestplan.host-check-findings.v1\0"
            + blocking_findings_json.encode("utf-8")
        ).hexdigest()
        owner_id = _require_text(owner_id, "owner_id", maximum=256)
        fencing_token = _require_nonnegative_int(fencing_token, "fencing_token")
        operation_id = _require_text(operation_id, "operation_id", maximum=256)

        def write(connection: sqlite3.Connection) -> StoredHostCheckFailure:
            job = self._require_lease(
                connection,
                job_id=job_id,
                owner_id=owner_id,
                fencing_token=fencing_token,
            )
            try:
                adapter_state = json.loads(str(job["adapter_state_json"]))
            except (json.JSONDecodeError, TypeError):
                adapter_state = None
            if (
                job["state"] != "queued"
                or job["current_generation"] is not None
                or not isinstance(adapter_state, dict)
                or not isinstance(
                    adapter_state.get("initial_check_pending"), dict
                )
                or job["source_kind"] != target.source_kind
                or job["source_id"] != target.plan_id
                or job["policy_digest"] != target.policy_digest
            ):
                raise ReviewStoreConflict("initial check failure checkpoint is stale")
            connection.execute(
                """
                INSERT INTO review_generations (
                    job_id, generation, state, target_digest, integration_oid,
                    check_receipt_digest, target_json
                ) VALUES (?, 0, 'blocked', ?, ?, ?, ?)
                """,
                (
                    job_id,
                    target.target_digest,
                    target.integration_oid,
                    target.check_receipt_digest,
                    target.canonical_json,
                ),
            )
            connection.execute(
                """
                INSERT INTO review_host_check_failures (
                    job_id, generation, target_digest, integration_oid,
                    check_failure_digest, blocking_findings_json,
                    blocking_findings_digest, operation_id, fencing_token
                ) VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    target.target_digest,
                    target.integration_oid,
                    check_failure_digest,
                    blocking_findings_json,
                    blocking_findings_digest,
                    operation_id,
                    fencing_token,
                ),
            )
            if connection.execute(
                """
                UPDATE review_jobs
                SET state='blocked', current_generation=0,
                    target_digest=?, integration_oid=?, check_receipt_digest=?
                WHERE job_id=? AND state='queued' AND current_generation IS NULL
                  AND owner_id=? AND fencing_token=? AND cancel_requested=0
                """,
                (
                    target.target_digest,
                    target.integration_oid,
                    target.check_receipt_digest,
                    job_id,
                    owner_id,
                    fencing_token,
                ),
            ).rowcount != 1:
                raise ReviewLeaseConflict(
                    "initial check failure lost its owner lease"
                )
            self._append_event_conn(
                connection,
                job_id=job_id,
                generation=0,
                owner_id=owner_id,
                fencing_token=fencing_token,
                operation_id=operation_id,
                kind="host_check_failed",
                target_digest=target.target_digest,
                payload={
                    "blocking_findings_digest": blocking_findings_digest,
                    "check_failure_digest": check_failure_digest,
                },
            )
            stored = connection.execute(
                "SELECT * FROM review_host_check_failures "
                "WHERE job_id=? AND generation=0",
                (job_id,),
            ).fetchone()
            if stored is None:
                raise ReviewStoreConflict("initial check failure was not stored")
            return self._host_check_failure_from_row(stored)

        return self._write(write)  # type: ignore[return-value]

    def get_job(self, job_id: str) -> ReviewJob:
        job_id = _require_text(job_id, "job_id", maximum=256)
        with self._connect() as connection:
            return self._job_from_row(self._job_row(connection, job_id))

    def finalize_expired_manual_cancellations(
        self,
        *,
        owner_profile: str,
        now_ns: int,
    ) -> tuple[ReviewJob, ...]:
        """Finalize cancelled manual jobs whose prior process lease expired."""

        owner_profile = _require_text(
            owner_profile, "owner_profile", maximum=256
        )
        now_ns = _require_nonnegative_int(now_ns, "now_ns")

        def write(connection: sqlite3.Connection) -> tuple[ReviewJob, ...]:
            rows = connection.execute(
                """
                SELECT * FROM review_jobs
                WHERE adapter_version=? AND source_kind='manual_snapshot'
                  AND owner_profile=? AND current_generation IS NOT NULL
                  AND state='cancel_requested' AND cancel_requested=1
                  AND owner_id IS NOT NULL
                  AND lease_expires_at_ns IS NOT NULL
                  AND lease_expires_at_ns < ?
                ORDER BY job_id
                """,
                (_MANUAL_ADAPTER_VERSION, owner_profile, now_ns),
            ).fetchall()
            finalized: list[ReviewJob] = []
            for row in rows:
                job_id = str(row["job_id"])
                owner_id = str(row["owner_id"])
                fencing_token = int(row["fencing_token"])
                lease_expires_at_ns = int(row["lease_expires_at_ns"])
                changed = connection.execute(
                    """
                    UPDATE review_jobs SET state='cancelled'
                    WHERE job_id=? AND adapter_version=?
                      AND source_kind='manual_snapshot' AND owner_profile=?
                      AND current_generation IS NOT NULL
                      AND state='cancel_requested' AND cancel_requested=1
                      AND owner_id=? AND fencing_token=?
                      AND lease_expires_at_ns=? AND lease_expires_at_ns < ?
                    """,
                    (
                        job_id,
                        _MANUAL_ADAPTER_VERSION,
                        owner_profile,
                        owner_id,
                        fencing_token,
                        lease_expires_at_ns,
                        now_ns,
                    ),
                ).rowcount
                if changed != 1:
                    raise ReviewLeaseConflict(
                        "manual startup cancellation lost its expired owner fence"
                    )
                self._append_event_conn(
                    connection,
                    job_id=job_id,
                    generation=int(row["current_generation"]),
                    owner_id=owner_id,
                    fencing_token=fencing_token,
                    operation_id=(
                        f"manual-startup-cancel-finalized:{fencing_token}"
                    ),
                    kind="cancelled",
                    target_digest=str(row["target_digest"]),
                    payload={
                        "children_extinct": True,
                        "recovered_after_owner_exit": True,
                    },
                )
                finalized.append(
                    self._job_from_row(self._job_row(connection, job_id))
                )
            return tuple(finalized)

        return self._write(write)  # type: ignore[return-value]

    def list_recoverable_manual_jobs(
        self,
        *,
        owner_profile: str,
        now_ns: int,
    ) -> tuple[ReviewJob, ...]:
        """List expired exact-profile manual jobs from this configured store."""

        owner_profile = _require_text(
            owner_profile, "owner_profile", maximum=256
        )
        now_ns = _require_nonnegative_int(now_ns, "now_ns")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM review_jobs
                WHERE adapter_version=? AND source_kind='manual_snapshot'
                  AND owner_profile=? AND current_generation IS NOT NULL
                  AND cancel_requested=0
                  AND state IN (
                    'reviewing', 'blocked', 'checking', 'repairing', 'waiting'
                  )
                  AND (
                    owner_id IS NULL OR lease_expires_at_ns IS NULL
                    OR lease_expires_at_ns < ?
                  )
                ORDER BY job_id
                """,
                (_MANUAL_ADAPTER_VERSION, owner_profile, now_ns),
            ).fetchall()
            recoverable: list[ReviewJob] = []
            for row in rows:
                if row["state"] != "repairing":
                    recoverable.append(self._job_from_row(row))
                    continue
                generation = int(row["current_generation"])
                prepared = connection.execute(
                    """
                    SELECT 1 FROM review_manual_checkpoints
                    WHERE job_id=? AND generation=? AND phase='repair_prepared'
                    """,
                    (str(row["job_id"]), generation + 1),
                ).fetchone()
                if prepared is not None:
                    recoverable.append(self._job_from_row(row))
                    continue
                # No repair checkpoint means the isolated repair never reached
                # live-file adoption. The exact blocker receipt is still the
                # durable repair input, so return to that checkpoint and let a
                # fresh fenced invocation retry. Only an absent or ambiguous
                # blocker is an integrity failure.
                model_blocker = connection.execute(
                    """
                    SELECT 1 FROM review_blocked_receipts
                    WHERE job_id=? AND generation=?
                    """,
                    (str(row["job_id"]), generation),
                ).fetchone()
                host_blocker = connection.execute(
                    """
                    SELECT 1 FROM review_host_check_failures
                    WHERE job_id=? AND generation=?
                    """,
                    (str(row["job_id"]), generation),
                ).fetchone()
                if (model_blocker is None) != (host_blocker is None):
                    changed = connection.execute(
                        """
                        UPDATE review_jobs
                        SET state='blocked', owner_id=NULL,
                            lease_expires_at_ns=NULL
                        WHERE job_id=? AND state='repairing'
                          AND fencing_token=? AND cancel_requested=0
                        """,
                        (str(row["job_id"]), int(row["fencing_token"])),
                    ).rowcount
                    if changed != 1:
                        raise ReviewStoreConflict(
                            "manual repair retry transition lost its fence"
                        )
                    connection.execute(
                        """
                        UPDATE review_generations SET state='blocked'
                        WHERE job_id=? AND generation=? AND state='repairing'
                        """,
                        (str(row["job_id"]), generation),
                    )
                    self._append_event_conn(
                        connection,
                        job_id=str(row["job_id"]),
                        generation=generation,
                        owner_id=str(row["owner_id"] or "manual-recovery"),
                        fencing_token=int(row["fencing_token"]),
                        operation_id=(
                            f"manual:{generation}:repair-retry:"
                            f"{int(row['fencing_token'])}"
                        ),
                        kind="manual_repair_retry_queued",
                        target_digest=str(row["target_digest"]),
                        payload={
                            "reason_code": "process_lost_before_checkpoint"
                        },
                    )
                    refreshed = self._job_row(connection, str(row["job_id"]))
                    recoverable.append(self._job_from_row(refreshed))
                    continue
                changed = connection.execute(
                    """
                    UPDATE review_jobs
                    SET state='failed_integrity', owner_id=NULL,
                        lease_expires_at_ns=NULL
                    WHERE job_id=? AND state='repairing'
                      AND fencing_token=? AND cancel_requested=0
                    """,
                    (str(row["job_id"]), int(row["fencing_token"])),
                ).rowcount
                if changed != 1:
                    raise ReviewStoreConflict(
                        "manual recovery integrity transition lost its fence"
                    )
                connection.execute(
                    """
                    UPDATE review_generations SET state='failed_integrity'
                    WHERE job_id=? AND generation=?
                    """,
                    (str(row["job_id"]), generation),
                )
                self._append_event_conn(
                    connection,
                    job_id=str(row["job_id"]),
                    generation=generation,
                    owner_id=str(row["owner_id"] or "manual-recovery"),
                    fencing_token=int(row["fencing_token"]),
                    operation_id=(
                        f"manual:{generation}:recovery-integrity:"
                        "missing-repair-checkpoint"
                    ),
                    kind="manual_recovery_integrity_failed",
                    target_digest=str(row["target_digest"]),
                    payload={"reason_code": "missing_repair_checkpoint"},
                )
            connection.commit()
        return tuple(recoverable)

    def terminalize_manual_recovery_integrity(
        self,
        *,
        job_id: str,
        reason_code: str,
    ) -> ReviewJob:
        """Fail one exact manual recovery whose durable evidence is corrupt."""

        job_id = _require_text(job_id, "job_id", maximum=256)
        reason_code = _require_text(
            reason_code, "manual integrity reason", maximum=128
        )

        def write(connection: sqlite3.Connection) -> ReviewJob:
            row = self._job_row(connection, job_id)
            if (
                row["adapter_version"] != _MANUAL_ADAPTER_VERSION
                or row["source_kind"] != "manual_snapshot"
                or row["current_generation"] is None
            ):
                raise ReviewStoreConflict(
                    "manual integrity failure targets a different job"
                )
            if row["state"] == "failed_integrity":
                return self._job_from_row(row)
            if row["state"] in {
                "passed",
                "cancel_requested",
                "cancelled",
                "landing_prepared",
                "landing_claimed",
                "landed",
            } or bool(row["cancel_requested"]):
                raise ReviewStoreConflict(
                    "manual integrity failure cannot replace a terminal state"
                )
            generation = int(row["current_generation"])
            connection.execute(
                """
                UPDATE review_jobs
                SET state='failed_integrity', owner_id=NULL,
                    lease_expires_at_ns=NULL
                WHERE job_id=? AND fencing_token=? AND cancel_requested=0
                """,
                (job_id, int(row["fencing_token"])),
            )
            connection.execute(
                """
                UPDATE review_generations SET state='failed_integrity'
                WHERE job_id=? AND generation=?
                """,
                (job_id, generation),
            )
            self._append_event_conn(
                connection,
                job_id=job_id,
                generation=generation,
                owner_id=str(row["owner_id"] or "manual-recovery"),
                fencing_token=int(row["fencing_token"]),
                operation_id=(
                    f"manual:{generation}:recovery-integrity:{reason_code}"
                ),
                kind="manual_recovery_integrity_failed",
                target_digest=str(row["target_digest"]),
                payload={"reason_code": reason_code},
            )
            return self._job_from_row(self._job_row(connection, job_id))

        return self._write(write)  # type: ignore[return-value]

    def record_manual_checkpoint(
        self,
        *,
        job_id: str,
        prior_generation: int,
        generation: int,
        phase: str,
        prior_target_digest: str,
        snapshot_digest: str,
        live_state_digest: str,
        payload: Mapping[str, object],
        owner_id: str,
        fencing_token: int,
        operation_id: str,
    ) -> StoredManualCheckpoint:
        """Freeze one manual post-repair or post-check recovery boundary."""

        job_id = _require_text(job_id, "job_id", maximum=256)
        prior_generation = _require_nonnegative_int(
            prior_generation, "prior_generation"
        )
        generation = _require_nonnegative_int(generation, "generation")
        if generation != prior_generation + 1:
            raise ReviewValidationError(
                "manual checkpoint generation must be consecutive"
            )
        phase = _require_text(phase, "manual checkpoint phase", maximum=32)
        if phase not in {
            "repair_prepared",
            "repair_applied",
            "checks_passed",
        }:
            raise ReviewValidationError("manual checkpoint phase is invalid")
        prior_target_digest = _require_digest(
            prior_target_digest, "prior_target_digest"
        )
        snapshot_digest = _require_digest(snapshot_digest, "snapshot_digest")
        live_state_digest = _require_digest(
            live_state_digest, "live_state_digest"
        )
        payload_json = _bounded_canonical_json(
            dict(payload), "manual checkpoint payload", expected_type=dict
        )
        payload_digest = _domain_digest(
            b"hermes.manual-review-checkpoint.v1\0", payload_json
        )
        owner_id = _require_text(owner_id, "owner_id", maximum=256)
        fencing_token = _require_nonnegative_int(
            fencing_token, "fencing_token"
        )
        operation_id = _require_text(
            operation_id, "operation_id", maximum=256
        )

        def write(connection: sqlite3.Connection) -> StoredManualCheckpoint:
            job = self._require_lease(
                connection,
                job_id=job_id,
                owner_id=owner_id,
                fencing_token=fencing_token,
            )
            if (
                job["adapter_version"] != _MANUAL_ADAPTER_VERSION
                or job["source_kind"] != "manual_snapshot"
                or job["current_generation"] != prior_generation
                or job["target_digest"] != prior_target_digest
                or job["state"] not in {"blocked", "repairing", "checking"}
            ):
                raise ReviewStoreConflict(
                    "manual checkpoint target is stale"
                )
            existing = connection.execute(
                """
                SELECT * FROM review_manual_checkpoints
                WHERE job_id=? AND generation=? AND phase=?
                """,
                (job_id, generation, phase),
            ).fetchone()
            expected = (
                prior_generation,
                prior_target_digest,
                snapshot_digest,
                live_state_digest,
                payload_json,
                payload_digest,
                operation_id,
                fencing_token,
            )
            if existing is not None:
                actual = tuple(
                    existing[name]
                    for name in (
                        "prior_generation",
                        "prior_target_digest",
                        "snapshot_digest",
                        "live_state_digest",
                        "payload_json",
                        "payload_digest",
                        "operation_id",
                        "fencing_token",
                    )
                )
                if actual != expected:
                    raise ReviewStoreConflict(
                        "manual checkpoint conflicts with immutable evidence"
                    )
                return self._manual_checkpoint_from_row(existing)
            if phase in {"repair_applied", "checks_passed"}:
                repair = connection.execute(
                    """
                    SELECT * FROM review_manual_checkpoints
                    WHERE job_id=? AND generation=? AND phase=?
                    """,
                    (
                        job_id,
                        generation,
                        "repair_prepared"
                        if phase == "repair_applied"
                        else "repair_applied",
                    ),
                ).fetchone()
                if (
                    repair is None
                    or repair["snapshot_digest"] != snapshot_digest
                    or repair["live_state_digest"] != live_state_digest
                ):
                    raise ReviewStoreConflict(
                        "manual checks checkpoint has no exact repair"
                    )
            connection.execute(
                """
                INSERT INTO review_manual_checkpoints (
                    job_id, prior_generation, generation, phase,
                    prior_target_digest, snapshot_digest, live_state_digest,
                    payload_json, payload_digest, operation_id, fencing_token
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    prior_generation,
                    generation,
                    phase,
                    prior_target_digest,
                    snapshot_digest,
                    live_state_digest,
                    payload_json,
                    payload_digest,
                    operation_id,
                    fencing_token,
                ),
            )
            next_state = "checking"
            connection.execute(
                "UPDATE review_jobs SET state=? WHERE job_id=?",
                (next_state, job_id),
            )
            connection.execute(
                "UPDATE review_generations SET state=? "
                "WHERE job_id=? AND generation=?",
                (next_state, job_id, prior_generation),
            )
            self._append_event_conn(
                connection,
                job_id=job_id,
                generation=prior_generation,
                owner_id=owner_id,
                fencing_token=fencing_token,
                operation_id=operation_id,
                kind=f"manual_{phase}",
                target_digest=prior_target_digest,
                payload={
                    "generation": generation,
                    "payload_digest": payload_digest,
                    "snapshot_digest": snapshot_digest,
                },
            )
            stored = connection.execute(
                """
                SELECT * FROM review_manual_checkpoints
                WHERE job_id=? AND generation=? AND phase=?
                """,
                (job_id, generation, phase),
            ).fetchone()
            if stored is None:
                raise ReviewStoreConflict(
                    "manual checkpoint was not stored"
                )
            return self._manual_checkpoint_from_row(stored)

        return self._write(write)  # type: ignore[return-value]

    def get_manual_checkpoint(
        self,
        *,
        job_id: str,
        generation: int,
        phase: str,
    ) -> StoredManualCheckpoint | None:
        job_id = _require_text(job_id, "job_id", maximum=256)
        generation = _require_nonnegative_int(generation, "generation")
        phase = _require_text(phase, "manual checkpoint phase", maximum=32)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM review_manual_checkpoints
                WHERE job_id=? AND generation=? AND phase=?
                """,
                (job_id, generation, phase),
            ).fetchone()
        if row is None:
            return None
        checkpoint = self._manual_checkpoint_from_row(row)
        if checkpoint.payload_digest != _domain_digest(
            b"hermes.manual-review-checkpoint.v1\0",
            checkpoint.payload_json,
        ):
            raise ReviewStoreConflict("manual checkpoint digest differs")
        return checkpoint

    def get_generation(self, job_id: str, generation: int) -> ReviewGeneration:
        job_id = _require_text(job_id, "job_id", maximum=256)
        generation = _require_nonnegative_int(generation, "generation")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM review_generations WHERE job_id=? AND generation=?",
                (job_id, generation),
            ).fetchone()
        if row is None:
            raise ReviewValidationError("review generation does not exist")
        return self._generation_from_row(row)

    def find_active_bestplan_job(
        self,
        *,
        owner_session_id: str,
        owner_profile: str,
        workspace: str | Path,
        hinted_job_id: str | None = None,
    ) -> ReviewJob | None:
        """Resolve one session-owned open BestPlan review, or fail on ambiguity."""

        owner_session_id = _require_text(
            owner_session_id, "owner_session_id", maximum=512
        )
        owner_profile = _require_text(owner_profile, "owner_profile", maximum=256)
        try:
            canonical_workspace = str(Path(workspace).expanduser().resolve(strict=True))
        except (OSError, RuntimeError, TypeError) as exc:
            raise ReviewValidationError("review workspace is invalid") from exc
        if hinted_job_id is not None:
            hinted_job_id = _require_text(hinted_job_id, "job_id", maximum=256)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM review_jobs
                WHERE adapter_version='local-bestplan.v1'
                  AND source_kind='bestplan_integration'
                  AND owner_session_id=? AND owner_profile=? AND workspace=?
                  AND current_generation IS NOT NULL
                  AND cancel_requested=0
                  AND state NOT IN ('cancelled', 'landing_prepared', 'landed')
                ORDER BY job_id
                """,
                (owner_session_id, owner_profile, canonical_workspace),
            ).fetchall()
        jobs = tuple(self._job_from_row(row) for row in rows)
        if hinted_job_id is not None:
            hinted = tuple(job for job in jobs if job.job_id == hinted_job_id)
            if hinted:
                return hinted[0]
            raise ReviewStoreConflict("named active BestPlan review target is stale")
        if not jobs:
            return None
        if len(jobs) != 1:
            raise ReviewStoreConflict("active BestPlan review target is ambiguous")
        return jobs[0]

    def claim_job(
        self,
        *,
        job_id: str,
        owner_id: str,
        now_ns: int,
        lease_duration_ns: int,
        expected_fencing_token: int,
    ) -> ReviewJob:
        job_id = _require_text(job_id, "job_id", maximum=256)
        owner_id = _require_text(owner_id, "owner_id", maximum=256)
        now_ns = _require_nonnegative_int(now_ns, "now_ns")
        lease_duration_ns = _require_positive_int(
            lease_duration_ns, "lease_duration_ns"
        )
        expected_fencing_token = _require_nonnegative_int(
            expected_fencing_token, "expected_fencing_token"
        )
        expires = now_ns + lease_duration_ns
        if expires > (1 << 63) - 1:
            raise ReviewValidationError("review lease expiry is out of range")

        def write(connection: sqlite3.Connection) -> ReviewJob:
            row = self._job_row(connection, job_id)
            current_token = int(row["fencing_token"])
            if current_token != expected_fencing_token:
                raise ReviewLeaseConflict("review claim fencing token changed")
            if bool(row["cancel_requested"]):
                raise ReviewLeaseConflict("cancelled review job cannot be claimed")
            if row["state"] == "landing_claimed":
                raise ReviewLeaseConflict("claimed landing requires effect recovery")
            current_owner = row["owner_id"]
            current_expiry = row["lease_expires_at_ns"]
            if current_owner is not None and (
                current_expiry is None or now_ns <= int(current_expiry)
            ):
                raise ReviewLeaseConflict("review job has an active owner lease")
            next_token = current_token + 1
            if next_token > (1 << 63) - 1:
                raise ReviewLeaseConflict("review fencing token is exhausted")
            changed = connection.execute(
                """
                UPDATE review_jobs
                SET owner_id = ?, fencing_token = ?, lease_expires_at_ns = ?
                WHERE job_id = ? AND fencing_token = ? AND cancel_requested = 0
                """,
                (owner_id, next_token, expires, job_id, current_token),
            ).rowcount
            if changed != 1:
                raise ReviewLeaseConflict("review claim lost its compare-and-swap")
            return self._job_from_row(self._job_row(connection, job_id))

        return self._write(write)  # type: ignore[return-value]

    def renew_lease(
        self,
        *,
        job_id: str,
        owner_id: str,
        fencing_token: int,
        now_ns: int,
        lease_duration_ns: int,
    ) -> ReviewJob:
        job_id = _require_text(job_id, "job_id", maximum=256)
        owner_id = _require_text(owner_id, "owner_id", maximum=256)
        fencing_token = _require_nonnegative_int(fencing_token, "fencing_token")
        now_ns = _require_nonnegative_int(now_ns, "now_ns")
        lease_duration_ns = _require_positive_int(
            lease_duration_ns, "lease_duration_ns"
        )
        expires = now_ns + lease_duration_ns
        if expires > (1 << 63) - 1:
            raise ReviewValidationError("review lease expiry is out of range")

        def write(connection: sqlite3.Connection) -> ReviewJob:
            row = self._require_lease(
                connection,
                job_id=job_id,
                owner_id=owner_id,
                fencing_token=fencing_token,
            )
            if now_ns > int(row["lease_expires_at_ns"]):
                raise ReviewLeaseConflict("expired review lease cannot be renewed")
            changed = connection.execute(
                """
                UPDATE review_jobs SET lease_expires_at_ns = ?
                WHERE job_id = ? AND owner_id = ? AND fencing_token = ?
                  AND cancel_requested = 0
                """,
                (expires, job_id, owner_id, fencing_token),
            ).rowcount
            if changed != 1:
                raise ReviewLeaseConflict("review lease renewal lost ownership")
            return self._job_from_row(self._job_row(connection, job_id))

        return self._write(write)  # type: ignore[return-value]

    def allocate_manual_repair_attempt(
        self,
        *,
        job_id: str,
        generation: int,
        target_digest: str,
        owner_id: str,
        fencing_token: int,
        operation_id: str,
    ) -> int:
        """Consume one durable repair ordinal before worker side effects."""

        job_id = _require_text(job_id, "job_id", maximum=256)
        generation = _require_nonnegative_int(generation, "generation")
        target_digest = _require_digest(target_digest, "target_digest")
        owner_id = _require_text(owner_id, "owner_id", maximum=256)
        fencing_token = _require_nonnegative_int(
            fencing_token, "fencing_token"
        )
        operation_id = _require_text(operation_id, "operation_id", maximum=256)

        def write(connection: sqlite3.Connection) -> int:
            row = self._require_lease(
                connection,
                job_id=job_id,
                owner_id=owner_id,
                fencing_token=fencing_token,
            )
            if (
                row["adapter_version"] != _MANUAL_ADAPTER_VERSION
                or row["current_generation"] != generation
                or row["target_digest"] != target_digest
            ):
                raise ReviewStoreConflict(
                    "manual repair attempt target is stale"
                )
            attempt = int(row["next_manual_repair_attempt"]) + 1
            if attempt > (1 << 63) - 1:
                raise ReviewStoreConflict(
                    "manual repair attempt ordinal is exhausted"
                )
            if connection.execute(
                """
                UPDATE review_jobs SET next_manual_repair_attempt=?
                WHERE job_id=? AND owner_id=? AND fencing_token=?
                  AND next_manual_repair_attempt=? AND cancel_requested=0
                """,
                (
                    attempt,
                    job_id,
                    owner_id,
                    fencing_token,
                    attempt - 1,
                ),
            ).rowcount != 1:
                raise ReviewLeaseConflict(
                    "manual repair attempt allocation lost ownership"
                )
            self._append_event_conn(
                connection,
                job_id=job_id,
                generation=generation,
                owner_id=owner_id,
                fencing_token=fencing_token,
                operation_id=operation_id,
                kind="manual_repair_attempt_allocated",
                target_digest=target_digest,
                payload={"repair_attempt": attempt},
            )
            return attempt

        return self._write(write)  # type: ignore[return-value]

    @classmethod
    def _append_event_conn(
        cls,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        generation: int,
        owner_id: str,
        fencing_token: int,
        operation_id: str,
        kind: str,
        target_digest: str,
        payload: object,
    ) -> ReviewStoreEvent:
        payload_json = _canonical_json(payload)
        payload_digest = _domain_digest(_EVENT_PAYLOAD_DOMAIN, payload_json)
        existing_row = connection.execute(
            """
            SELECT * FROM review_store_events
            WHERE job_id = ? AND operation_id = ?
            """,
            (job_id, operation_id),
        ).fetchone()
        immutable = (
            generation,
            owner_id,
            fencing_token,
            kind,
            target_digest,
            payload_json,
            payload_digest,
        )
        if existing_row is not None:
            existing = cls._event_from_row(existing_row)
            current = (
                existing.generation,
                existing.owner_id,
                existing.fencing_token,
                existing.kind,
                existing.target_digest,
                existing.payload_json,
                existing.payload_digest,
            )
            if current != immutable:
                raise ReviewStoreConflict(
                    "review event operation conflicts with immutable data"
                )
            return existing
        previous = connection.execute(
            """
            SELECT event_seq, event_digest FROM review_store_events
            WHERE job_id = ? ORDER BY event_seq DESC LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        event_seq = 1 if previous is None else int(previous["event_seq"]) + 1
        previous_digest = None if previous is None else str(previous["event_digest"])
        event_body = {
            "event_seq": event_seq,
            "fencing_token": fencing_token,
            "generation": generation,
            "job_id": job_id,
            "kind": kind,
            "operation_id": operation_id,
            "owner_id": owner_id,
            "payload_digest": payload_digest,
            "previous_event_digest": previous_digest,
            "target_digest": target_digest,
        }
        event_digest = _domain_digest(
            _STORE_EVENT_DOMAIN, _canonical_json(event_body)
        )
        connection.execute(
            """
            INSERT INTO review_store_events (
                job_id, event_seq, generation, owner_id, fencing_token,
                operation_id, kind, target_digest, payload_json, payload_digest,
                previous_event_digest, event_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                event_seq,
                generation,
                owner_id,
                fencing_token,
                operation_id,
                kind,
                target_digest,
                payload_json,
                payload_digest,
                previous_digest,
                event_digest,
            ),
        )
        return ReviewStoreEvent(
            job_id=job_id,
            event_seq=event_seq,
            generation=generation,
            owner_id=owner_id,
            fencing_token=fencing_token,
            operation_id=operation_id,
            kind=kind,
            target_digest=target_digest,
            payload_json=payload_json,
            payload_digest=payload_digest,
            previous_event_digest=previous_digest,
            event_digest=event_digest,
        )

    def append_event(
        self,
        *,
        job_id: str,
        generation: int,
        owner_id: str,
        fencing_token: int,
        operation_id: str,
        kind: str,
        target_digest: str,
        payload: object,
    ) -> ReviewStoreEvent:
        job_id = _require_text(job_id, "job_id", maximum=256)
        generation = _require_nonnegative_int(generation, "generation")
        owner_id = _require_text(owner_id, "owner_id", maximum=256)
        fencing_token = _require_nonnegative_int(fencing_token, "fencing_token")
        operation_id = _require_text(operation_id, "operation_id", maximum=256)
        kind = _require_text(kind, "event kind", maximum=128)
        target_digest = _require_digest(target_digest, "target_digest")

        def write(connection: sqlite3.Connection) -> ReviewStoreEvent:
            self._require_lease(
                connection,
                job_id=job_id,
                owner_id=owner_id,
                fencing_token=fencing_token,
            )
            return self._append_event_conn(
                connection,
                job_id=job_id,
                generation=generation,
                owner_id=owner_id,
                fencing_token=fencing_token,
                operation_id=operation_id,
                kind=kind,
                target_digest=target_digest,
                payload=payload,
            )

        return self._write(write)  # type: ignore[return-value]

    def begin_generation(
        self,
        *,
        job_id: str,
        generation: int,
        target: ReviewTarget,
        artifact: ReviewArtifact | None = None,
        owner_id: str,
        fencing_token: int,
        operation_id: str,
    ) -> ReviewGeneration:
        job_id = _require_text(job_id, "job_id", maximum=256)
        generation = _require_nonnegative_int(generation, "generation")
        if not isinstance(target, ReviewTarget):
            raise ReviewValidationError("review generation target is invalid")
        if artifact is not None and (
            not isinstance(artifact, ReviewArtifact)
            or artifact.target_digest != target.target_digest
        ):
            raise ReviewValidationError("review generation artifact is invalid")
        artifact_json = None if artifact is None else artifact.canonical_json
        owner_id = _require_text(owner_id, "owner_id", maximum=256)
        fencing_token = _require_nonnegative_int(fencing_token, "fencing_token")
        operation_id = _require_text(operation_id, "operation_id", maximum=256)

        def write(connection: sqlite3.Connection) -> ReviewGeneration:
            job = self._require_lease(
                connection,
                job_id=job_id,
                owner_id=owner_id,
                fencing_token=fencing_token,
            )
            if (
                job["source_kind"] != target.source_kind
                or job["source_id"] != target.plan_id
                or job["policy_digest"] != target.policy_digest
            ):
                raise ReviewStoreConflict("review generation target differs from its job")
            current_generation = job["current_generation"]
            existing = connection.execute(
                """
                SELECT * FROM review_generations
                WHERE job_id = ? AND generation = ?
                """,
                (job_id, generation),
            ).fetchone()
            immutable = (
                target.target_digest,
                target.integration_oid,
                target.check_receipt_digest,
                target.canonical_json,
                artifact_json,
            )
            if existing is not None:
                current = tuple(
                    existing[name]
                    for name in (
                        "target_digest",
                        "integration_oid",
                        "check_receipt_digest",
                        "target_json",
                        "artifact_json",
                    )
                )
                if current != immutable:
                    raise ReviewStoreConflict(
                        "review generation conflicts with immutable target data"
                    )
                if current_generation is None or generation != int(current_generation):
                    raise ReviewStoreConflict("review generation is no longer current")
                return self._generation_from_row(existing)
            if current_generation is None:
                if generation != 0:
                    raise ReviewStoreConflict("first review generation must be zero")
                if (
                    job["target_digest"] != target.target_digest
                    or job["integration_oid"] != target.integration_oid
                    or job["check_receipt_digest"] != target.check_receipt_digest
                ):
                    raise ReviewStoreConflict(
                        "first review generation differs from its job target"
                    )
            elif generation != int(current_generation) + 1:
                raise ReviewStoreConflict("review generation must follow the current one")
            connection.execute(
                """
                INSERT INTO review_generations (
                    job_id, generation, state, target_digest, integration_oid,
                    check_receipt_digest, target_json, artifact_json
                ) VALUES (?, ?, 'reviewing', ?, ?, ?, ?, ?)
                """,
                (job_id, generation, *immutable),
            )
            changed = connection.execute(
                """
                UPDATE review_jobs
                SET state = 'reviewing', current_generation = ?,
                    target_digest = ?, integration_oid = ?,
                    check_receipt_digest = ?
                WHERE job_id = ? AND owner_id = ? AND fencing_token = ?
                  AND cancel_requested = 0
                """,
                (
                    generation,
                    target.target_digest,
                    target.integration_oid,
                    target.check_receipt_digest,
                    job_id,
                    owner_id,
                    fencing_token,
                ),
            ).rowcount
            if changed != 1:
                raise ReviewLeaseConflict("review generation lost its owner lease")
            self._append_event_conn(
                connection,
                job_id=job_id,
                generation=generation,
                owner_id=owner_id,
                fencing_token=fencing_token,
                operation_id=operation_id,
                kind="generation_started",
                target_digest=target.target_digest,
                payload={"target": json.loads(target.canonical_json)},
            )
            row = connection.execute(
                """
                SELECT * FROM review_generations
                WHERE job_id = ? AND generation = ?
                """,
                (job_id, generation),
            ).fetchone()
            if row is None:
                raise ReviewStoreConflict("review generation was not stored")
            return self._generation_from_row(row)

        return self._write(write)  # type: ignore[return-value]

    def record_reviewer_receipt(
        self,
        *,
        job_id: str,
        generation: int,
        slot: str,
        target_digest: str,
        integration_oid: str,
        output_digest: str,
        verdict_digest: str,
        passed: bool,
        receipt_json: str | None = None,
        owner_id: str,
        fencing_token: int,
        operation_id: str,
    ) -> StoredReviewerReceipt:
        job_id = _require_text(job_id, "job_id", maximum=256)
        generation = _require_nonnegative_int(generation, "generation")
        slot = _require_text(slot, "reviewer slot", maximum=64)
        if slot not in _REQUIRED_SLOTS:
            raise ReviewValidationError("reviewer slot is unsupported")
        target_digest = _require_digest(target_digest, "target_digest")
        integration_oid = _require_oid(integration_oid, "integration_oid")
        output_digest = _require_digest(output_digest, "output_digest")
        verdict_digest = _require_digest(verdict_digest, "verdict_digest")
        if not isinstance(passed, bool):
            raise ReviewValidationError("reviewer receipt pass state must be boolean")
        if receipt_json is None:
            receipt_json = _canonical_json(
                {
                    "integration_oid": integration_oid,
                    "output_digest": output_digest,
                    "passed": passed,
                    "schema": "hermes.bestplan.stored-reviewer-receipt.v1",
                    "slot": slot,
                    "target_digest": target_digest,
                    "verdict_digest": verdict_digest,
                }
            )
        else:
            receipt_json = _bounded_canonical_json(
                receipt_json,
                "receipt_json",
                expected_type=dict,
            )
        owner_id = _require_text(owner_id, "owner_id", maximum=256)
        fencing_token = _require_nonnegative_int(fencing_token, "fencing_token")
        operation_id = _require_text(operation_id, "operation_id", maximum=256)

        def write(connection: sqlite3.Connection) -> StoredReviewerReceipt:
            job = self._require_lease(
                connection,
                job_id=job_id,
                owner_id=owner_id,
                fencing_token=fencing_token,
            )
            if job["current_generation"] != generation or job["state"] != "reviewing":
                raise ReviewStoreConflict("reviewer receipt generation is not reviewing")
            target_row = connection.execute(
                """
                SELECT * FROM review_generations
                WHERE job_id = ? AND generation = ?
                """,
                (job_id, generation),
            ).fetchone()
            if (
                target_row is None
                or target_row["target_digest"] != target_digest
                or target_row["integration_oid"] != integration_oid
            ):
                raise ReviewStoreConflict("reviewer receipt target is stale")
            existing = connection.execute(
                """
                SELECT * FROM review_reviewer_receipts
                WHERE job_id = ? AND generation = ? AND slot = ?
                """,
                (job_id, generation, slot),
            ).fetchone()
            if existing is not None:
                current = tuple(
                    existing[name]
                    for name in (
                        "target_digest",
                        "integration_oid",
                        "output_digest",
                        "verdict_digest",
                        "passed",
                        "receipt_json",
                    )
                )
                expected = (
                    target_digest,
                    integration_oid,
                    output_digest,
                    verdict_digest,
                    int(passed),
                    receipt_json,
                )
                if current != expected:
                    raise ReviewStoreConflict(
                        "reviewer slot conflicts with immutable receipt evidence"
                    )
                return self._receipt_from_row(existing)
            connection.execute(
                """
                INSERT INTO review_reviewer_receipts (
                    job_id, generation, slot, target_digest, integration_oid,
                    output_digest, verdict_digest, passed, operation_id,
                    fencing_token, receipt_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    generation,
                    slot,
                    target_digest,
                    integration_oid,
                    output_digest,
                    verdict_digest,
                    int(passed),
                    operation_id,
                    fencing_token,
                    receipt_json,
                ),
            )
            self._append_event_conn(
                connection,
                job_id=job_id,
                generation=generation,
                owner_id=owner_id,
                fencing_token=fencing_token,
                operation_id=operation_id,
                kind="reviewer_receipt",
                target_digest=target_digest,
                payload={
                    "integration_oid": integration_oid,
                    "output_digest": output_digest,
                    "passed": passed,
                    "receipt_json_digest": hashlib.sha256(
                        receipt_json.encode("utf-8")
                    ).hexdigest(),
                    "slot": slot,
                    "verdict_digest": verdict_digest,
                },
            )
            row = connection.execute(
                """
                SELECT * FROM review_reviewer_receipts
                WHERE job_id = ? AND generation = ? AND slot = ?
                """,
                (job_id, generation, slot),
            ).fetchone()
            if row is None:
                raise ReviewStoreConflict("reviewer receipt was not stored")
            return self._receipt_from_row(row)

        return self._write(write)  # type: ignore[return-value]

    def record_generation_blocked(
        self,
        *,
        job_id: str,
        generation: int,
        target_digest: str,
        integration_oid: str,
        check_receipt_digest: str,
        review_receipt_digest: str,
        blocking_findings_json: str,
        owner_id: str,
        fencing_token: int,
        operation_id: str,
    ) -> StoredReviewBlocked:
        """Commit blockers before any repair child can start."""

        job_id = _require_text(job_id, "job_id", maximum=256)
        generation = _require_nonnegative_int(generation, "generation")
        target_digest = _require_digest(target_digest, "target_digest")
        integration_oid = _require_oid(integration_oid, "integration_oid")
        check_receipt_digest = _require_digest(
            check_receipt_digest, "check_receipt_digest"
        )
        review_receipt_digest = _require_digest(
            review_receipt_digest, "review_receipt_digest"
        )
        blocking_findings_json = _bounded_canonical_json(
            blocking_findings_json,
            "blocking_findings_json",
            expected_type=list,
        )
        if not json.loads(blocking_findings_json):
            raise ReviewValidationError("blocking findings must not be empty")
        blocking_findings_digest = hashlib.sha256(
            b"hermes.bestplan.blocking-findings.v1\0"
            + blocking_findings_json.encode("utf-8")
        ).hexdigest()
        owner_id = _require_text(owner_id, "owner_id", maximum=256)
        fencing_token = _require_nonnegative_int(fencing_token, "fencing_token")
        operation_id = _require_text(operation_id, "operation_id", maximum=256)

        def write(connection: sqlite3.Connection) -> StoredReviewBlocked:
            job = self._require_lease(
                connection,
                job_id=job_id,
                owner_id=owner_id,
                fencing_token=fencing_token,
            )
            existing = connection.execute(
                """
                SELECT * FROM review_blocked_receipts
                WHERE job_id=? AND generation=?
                """,
                (job_id, generation),
            ).fetchone()
            expected = (
                target_digest,
                integration_oid,
                check_receipt_digest,
                review_receipt_digest,
                blocking_findings_json,
                blocking_findings_digest,
                operation_id,
                fencing_token,
            )
            if existing is not None:
                actual = tuple(
                    existing[name]
                    for name in (
                        "target_digest",
                        "integration_oid",
                        "check_receipt_digest",
                        "review_receipt_digest",
                        "blocking_findings_json",
                        "blocking_findings_digest",
                        "operation_id",
                        "fencing_token",
                    )
                )
                if actual != expected:
                    raise ReviewStoreConflict(
                        "review blockers conflict with immutable evidence"
                    )
                return self._blocked_from_row(existing)
            generation_row = connection.execute(
                """
                SELECT * FROM review_generations
                WHERE job_id=? AND generation=?
                """,
                (job_id, generation),
            ).fetchone()
            receipts = connection.execute(
                """
                SELECT * FROM review_reviewer_receipts
                WHERE job_id=? AND generation=?
                """,
                (job_id, generation),
            ).fetchall()
            by_slot = {str(row["slot"]): row for row in receipts}
            if (
                job["state"] != "reviewing"
                or job["current_generation"] != generation
                or job["target_digest"] != target_digest
                or job["integration_oid"] != integration_oid
                or job["check_receipt_digest"] != check_receipt_digest
                or generation_row is None
                or generation_row["state"] != "reviewing"
                or generation_row["target_digest"] != target_digest
                or set(by_slot) != set(_REQUIRED_SLOTS)
                or not any(not bool(row["passed"]) for row in receipts)
            ):
                raise ReviewStoreConflict(
                    "review blockers require both exact reviewer receipts"
                )
            connection.execute(
                """
                INSERT INTO review_blocked_receipts (
                    job_id, generation, target_digest, integration_oid,
                    check_receipt_digest, review_receipt_digest,
                    blocking_findings_json, blocking_findings_digest,
                    operation_id, fencing_token
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, generation, *expected),
            )
            if connection.execute(
                """
                UPDATE review_generations SET state='blocked'
                WHERE job_id=? AND generation=? AND state='reviewing'
                """,
                (job_id, generation),
            ).rowcount != 1:
                raise ReviewStoreConflict("review blocker lost its generation state")
            if connection.execute(
                """
                UPDATE review_jobs SET state='blocked'
                WHERE job_id=? AND current_generation=? AND state='reviewing'
                  AND owner_id=? AND fencing_token=? AND cancel_requested=0
                """,
                (job_id, generation, owner_id, fencing_token),
            ).rowcount != 1:
                raise ReviewLeaseConflict("review blocker lost its owner lease")
            self._append_event_conn(
                connection,
                job_id=job_id,
                generation=generation,
                owner_id=owner_id,
                fencing_token=fencing_token,
                operation_id=operation_id,
                kind="review_blocked",
                target_digest=target_digest,
                payload={
                    "blocking_findings_digest": blocking_findings_digest,
                    "review_receipt_digest": review_receipt_digest,
                },
            )
            stored = connection.execute(
                """
                SELECT * FROM review_blocked_receipts
                WHERE job_id=? AND generation=?
                """,
                (job_id, generation),
            ).fetchone()
            if stored is None:
                raise ReviewStoreConflict("review blockers were not stored")
            return self._blocked_from_row(stored)

        return self._write(write)  # type: ignore[return-value]

    def record_host_check_failure(
        self,
        *,
        job_id: str,
        generation: int,
        target_digest: str,
        integration_oid: str,
        check_failure_digest: str,
        blocking_findings_json: str,
        owner_id: str,
        fencing_token: int,
        operation_id: str,
    ) -> StoredHostCheckFailure:
        """Store host check evidence as a repair input, never as model review."""

        job_id = _require_text(job_id, "job_id", maximum=256)
        generation = _require_nonnegative_int(generation, "generation")
        target_digest = _require_digest(target_digest, "target_digest")
        integration_oid = _require_oid(integration_oid, "integration_oid")
        check_failure_digest = _require_digest(
            check_failure_digest, "check_failure_digest"
        )
        blocking_findings_json = _bounded_canonical_json(
            blocking_findings_json,
            "blocking_findings_json",
            expected_type=list,
        )
        if not json.loads(blocking_findings_json):
            raise ReviewValidationError("check failure findings must not be empty")
        blocking_findings_digest = hashlib.sha256(
            b"hermes.bestplan.host-check-findings.v1\0"
            + blocking_findings_json.encode("utf-8")
        ).hexdigest()
        owner_id = _require_text(owner_id, "owner_id", maximum=256)
        fencing_token = _require_nonnegative_int(fencing_token, "fencing_token")
        operation_id = _require_text(operation_id, "operation_id", maximum=256)

        def write(connection: sqlite3.Connection) -> StoredHostCheckFailure:
            job = self._require_lease(
                connection,
                job_id=job_id,
                owner_id=owner_id,
                fencing_token=fencing_token,
            )
            existing = connection.execute(
                """
                SELECT * FROM review_host_check_failures
                WHERE job_id=? AND generation=?
                """,
                (job_id, generation),
            ).fetchone()
            expected = (
                target_digest,
                integration_oid,
                check_failure_digest,
                blocking_findings_json,
                blocking_findings_digest,
                operation_id,
                fencing_token,
            )
            if existing is not None:
                actual = tuple(
                    existing[name]
                    for name in (
                        "target_digest",
                        "integration_oid",
                        "check_failure_digest",
                        "blocking_findings_json",
                        "blocking_findings_digest",
                        "operation_id",
                        "fencing_token",
                    )
                )
                if actual != expected:
                    raise ReviewStoreConflict(
                        "host check failure conflicts with immutable evidence"
                    )
                return self._host_check_failure_from_row(existing)
            generation_row = connection.execute(
                """
                SELECT * FROM review_generations
                WHERE job_id=? AND generation=?
                """,
                (job_id, generation),
            ).fetchone()
            if (
                job["state"] != "reviewing"
                or job["current_generation"] != generation
                or job["target_digest"] != target_digest
                or job["integration_oid"] != integration_oid
                or generation_row is None
                or generation_row["state"] != "reviewing"
                or generation_row["target_digest"] != target_digest
            ):
                raise ReviewStoreConflict("host check failure target is stale")
            connection.execute(
                """
                INSERT INTO review_host_check_failures (
                    job_id, generation, target_digest, integration_oid,
                    check_failure_digest, blocking_findings_json,
                    blocking_findings_digest, operation_id, fencing_token
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, generation, *expected),
            )
            if connection.execute(
                """
                UPDATE review_generations SET state='blocked'
                WHERE job_id=? AND generation=? AND state='reviewing'
                """,
                (job_id, generation),
            ).rowcount != 1:
                raise ReviewStoreConflict(
                    "host check failure lost its generation state"
                )
            if connection.execute(
                """
                UPDATE review_jobs SET state='blocked'
                WHERE job_id=? AND current_generation=? AND state='reviewing'
                  AND owner_id=? AND fencing_token=? AND cancel_requested=0
                """,
                (job_id, generation, owner_id, fencing_token),
            ).rowcount != 1:
                raise ReviewLeaseConflict(
                    "host check failure lost its owner lease"
                )
            self._append_event_conn(
                connection,
                job_id=job_id,
                generation=generation,
                owner_id=owner_id,
                fencing_token=fencing_token,
                operation_id=operation_id,
                kind="host_check_failed",
                target_digest=target_digest,
                payload={
                    "blocking_findings_digest": blocking_findings_digest,
                    "check_failure_digest": check_failure_digest,
                },
            )
            row = connection.execute(
                """
                SELECT * FROM review_host_check_failures
                WHERE job_id=? AND generation=?
                """,
                (job_id, generation),
            ).fetchone()
            if row is None:
                raise ReviewStoreConflict("host check failure was not stored")
            return self._host_check_failure_from_row(row)

        return self._write(write)  # type: ignore[return-value]

    def record_repair_check_failure(
        self,
        *,
        job_id: str,
        generation: int,
        target: ReviewTarget,
        check_failure_digest: str,
        blocking_findings_json: str,
        owner_id: str,
        fencing_token: int,
        operation_id: str,
    ) -> StoredHostCheckFailure:
        """Freeze a failed repaired check as the next repair generation."""

        job_id = _require_text(job_id, "job_id", maximum=256)
        generation = _require_nonnegative_int(generation, "generation")
        if not isinstance(target, ReviewTarget) or target.generation != generation:
            raise ReviewValidationError("repair check failure target is invalid")
        check_failure_digest = _require_digest(
            check_failure_digest, "check_failure_digest"
        )
        if target.check_receipt_digest != check_failure_digest:
            raise ReviewValidationError("repair check failure digest differs")
        blocking_findings_json = _bounded_canonical_json(
            blocking_findings_json,
            "blocking_findings_json",
            expected_type=list,
        )
        if not json.loads(blocking_findings_json):
            raise ReviewValidationError("check failure findings must not be empty")
        blocking_findings_digest = hashlib.sha256(
            b"hermes.bestplan.host-check-findings.v1\0"
            + blocking_findings_json.encode("utf-8")
        ).hexdigest()
        owner_id = _require_text(owner_id, "owner_id", maximum=256)
        fencing_token = _require_nonnegative_int(fencing_token, "fencing_token")
        operation_id = _require_text(operation_id, "operation_id", maximum=256)

        def write(connection: sqlite3.Connection) -> StoredHostCheckFailure:
            job = self._require_lease(
                connection,
                job_id=job_id,
                owner_id=owner_id,
                fencing_token=fencing_token,
            )
            existing = connection.execute(
                "SELECT * FROM review_host_check_failures "
                "WHERE job_id=? AND generation=?",
                (job_id, generation),
            ).fetchone()
            expected = (
                target.target_digest,
                target.integration_oid,
                check_failure_digest,
                blocking_findings_json,
                blocking_findings_digest,
                operation_id,
                fencing_token,
            )
            if existing is not None:
                actual = tuple(
                    existing[name]
                    for name in (
                        "target_digest",
                        "integration_oid",
                        "check_failure_digest",
                        "blocking_findings_json",
                        "blocking_findings_digest",
                        "operation_id",
                        "fencing_token",
                    )
                )
                if actual != expected:
                    raise ReviewStoreConflict(
                        "host check failure conflicts with immutable evidence"
                    )
                return self._host_check_failure_from_row(existing)
            repair = connection.execute(
                "SELECT * FROM review_repair_checkpoints "
                "WHERE job_id=? AND generation=?",
                (job_id, generation),
            ).fetchone()
            if (
                job["state"] != "checking"
                or job["current_generation"] != generation - 1
                or job["source_kind"] != target.source_kind
                or job["source_id"] != target.plan_id
                or job["policy_digest"] != target.policy_digest
                or repair is None
                or repair["integration_oid"] != target.integration_oid
                or repair["integration_tree_oid"] != target.integration_tree_oid
                or repair["integration_ref"] != target.integration_ref
                or repair["integration_receipt_digest"]
                != target.integration_receipt_digest
            ):
                raise ReviewStoreConflict("repair check failure target is stale")
            connection.execute(
                """
                INSERT INTO review_generations (
                    job_id, generation, state, target_digest, integration_oid,
                    check_receipt_digest, target_json
                ) VALUES (?, ?, 'blocked', ?, ?, ?, ?)
                """,
                (
                    job_id,
                    generation,
                    target.target_digest,
                    target.integration_oid,
                    target.check_receipt_digest,
                    target.canonical_json,
                ),
            )
            connection.execute(
                """
                INSERT INTO review_host_check_failures (
                    job_id, generation, target_digest, integration_oid,
                    check_failure_digest, blocking_findings_json,
                    blocking_findings_digest, operation_id, fencing_token
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, generation, *expected),
            )
            if connection.execute(
                """
                UPDATE review_jobs
                SET state='blocked', current_generation=?, target_digest=?,
                    integration_oid=?, check_receipt_digest=?
                WHERE job_id=? AND current_generation=? AND state='checking'
                  AND owner_id=? AND fencing_token=? AND cancel_requested=0
                """,
                (
                    generation,
                    target.target_digest,
                    target.integration_oid,
                    check_failure_digest,
                    job_id,
                    generation - 1,
                    owner_id,
                    fencing_token,
                ),
            ).rowcount != 1:
                raise ReviewLeaseConflict(
                    "repair check failure lost its owner lease"
                )
            self._append_event_conn(
                connection,
                job_id=job_id,
                generation=generation,
                owner_id=owner_id,
                fencing_token=fencing_token,
                operation_id=operation_id,
                kind="host_check_failed",
                target_digest=target.target_digest,
                payload={
                    "blocking_findings_digest": blocking_findings_digest,
                    "check_failure_digest": check_failure_digest,
                },
            )
            stored = connection.execute(
                "SELECT * FROM review_host_check_failures "
                "WHERE job_id=? AND generation=?",
                (job_id, generation),
            ).fetchone()
            if stored is None:
                raise ReviewStoreConflict("host check failure was not stored")
            return self._host_check_failure_from_row(stored)

        return self._write(write)  # type: ignore[return-value]

    def record_repair_candidate_frozen(
        self,
        *,
        job_id: str,
        prior_generation: int,
        prior_target_digest: str,
        base_integration_oid: str,
        manifest_slice_id: str,
        repair_attempt: int,
        attempt_plan_id: str,
        candidate_receipt_json: str,
        changed_paths_json: str,
        owner_id: str,
        fencing_token: int,
        operation_id: str,
    ) -> StoredRepairCandidate:
        """Store one frozen repair candidate before any later slice runs."""

        job_id = _require_text(job_id, "job_id", maximum=256)
        prior_generation = _require_nonnegative_int(
            prior_generation, "prior_generation"
        )
        prior_target_digest = _require_digest(
            prior_target_digest, "prior_target_digest"
        )
        base_integration_oid = _require_oid(
            base_integration_oid, "base_integration_oid"
        )
        manifest_slice_id = _require_text(
            manifest_slice_id, "manifest_slice_id", maximum=1024
        )
        repair_attempt = _require_nonnegative_int(
            repair_attempt, "repair_attempt"
        )
        attempt_plan_id = _require_text(
            attempt_plan_id, "attempt_plan_id", maximum=64
        )
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", attempt_plan_id) is None:
            raise ReviewValidationError("attempt_plan_id is invalid")
        candidate_receipt_json = _bounded_canonical_json(
            candidate_receipt_json,
            "candidate_receipt_json",
            expected_type=dict,
        )
        changed_paths_json = _bounded_canonical_json(
            changed_paths_json,
            "changed_paths_json",
            expected_type=list,
        )
        receipt = json.loads(candidate_receipt_json)
        changed_paths = json.loads(changed_paths_json)
        receipt_fields = {
            "admitted",
            "candidate_expires_at",
            "candidate_id",
            "candidate_ref",
            "changed_paths",
            "commit_oid",
            "controller",
            "attempt_id",
            "manifest_slice_id",
            "policy_digest",
            "promotion_contract_digest",
            "schema",
            "slice_id",
            "tree_oid",
            "worker_receipt",
            "worker_receipt_sha256",
        }
        if set(receipt) != receipt_fields or receipt.get("schema") != (
            "hermes.bestplan.host-candidate-receipt.v1"
        ):
            raise ReviewValidationError("repair candidate receipt is invalid")
        if receipt.get("manifest_slice_id") != manifest_slice_id:
            raise ReviewValidationError("repair candidate slice differs")
        for name in ("candidate_id", "slice_id", "attempt_id"):
            value = receipt.get(name)
            if (
                not isinstance(value, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value)
                is None
            ):
                raise ReviewValidationError(
                    f"repair candidate {name} is invalid"
                )
        expected_ref = (
            f"refs/hermes-bestplan/{attempt_plan_id}/"
            f"{receipt['slice_id']}/{receipt['attempt_id']}"
        )
        if receipt.get("candidate_ref") != expected_ref:
            raise ReviewValidationError("repair candidate ref is invalid")
        _require_oid(receipt.get("commit_oid"), "candidate commit_oid")
        _require_oid(receipt.get("tree_oid"), "candidate tree_oid")
        _require_digest(receipt.get("policy_digest"), "candidate policy_digest")
        _require_digest(
            receipt.get("promotion_contract_digest"),
            "candidate promotion_contract_digest",
        )
        _require_digest(
            receipt.get("worker_receipt_sha256"),
            "candidate worker_receipt_sha256",
        )
        expires_at = receipt.get("candidate_expires_at")
        if isinstance(expires_at, bool) or not isinstance(expires_at, int) or expires_at < 1:
            raise ReviewValidationError("repair candidate expiry is invalid")
        controller = receipt.get("controller")
        if not isinstance(controller, dict) or set(controller) != {
            "id", "repository_id", "release_oid", "artifact_sha256",
        }:
            raise ReviewValidationError("repair candidate controller is invalid")
        _require_text(controller.get("id"), "candidate controller id", maximum=256)
        _require_text(
            controller.get("repository_id"),
            "candidate controller repository_id",
            maximum=256,
        )
        _require_oid(controller.get("release_oid"), "candidate controller release_oid")
        _require_digest(
            controller.get("artifact_sha256"),
            "candidate controller artifact_sha256",
        )
        admitted = receipt.get("admitted")
        if not isinstance(admitted, dict) or set(admitted) != {
            "requests", "input_tokens", "output_tokens",
        }:
            raise ReviewValidationError("repair candidate usage is invalid")
        for name, value in admitted.items():
            _require_nonnegative_int(value, f"candidate admitted {name}")
        worker_receipt = receipt.get("worker_receipt")
        if not isinstance(worker_receipt, dict):
            raise ReviewValidationError("repair candidate worker receipt is invalid")
        worker_receipt_bytes = json.dumps(
            worker_receipt,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if hashlib.sha256(worker_receipt_bytes).hexdigest() != receipt.get(
            "worker_receipt_sha256"
        ):
            raise ReviewValidationError("repair candidate worker receipt differs")
        if not changed_paths or not all(isinstance(path, str) for path in changed_paths):
            raise ReviewValidationError("repair candidate changed paths are invalid")
        changed_raw: list[bytes] = []
        for value in changed_paths:
            if (
                not value
                or "\\" in value
                or "\x00" in value
                or PurePosixPath(value).is_absolute()
                or any(part in {"", ".", ".."} for part in PurePosixPath(value).parts)
            ):
                raise ReviewValidationError(
                    "repair candidate changed path is invalid"
                )
            changed_raw.append(value.encode("utf-8", "strict"))
        normalized = tuple(sorted(changed_raw))
        if len(set(normalized)) != len(normalized):
            raise ReviewValidationError("repair candidate changed paths differ")
        changed_payload = json.dumps(
            [path.hex() for path in normalized],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        expected_changed_digest = hashlib.sha256(
            _BESTPLAN_CHANGED_PATHS_DOMAIN + changed_payload
        ).hexdigest()
        changed_summary = receipt.get("changed_paths")
        if (
            not isinstance(changed_summary, dict)
            or set(changed_summary) != {"count", "sha256"}
            or changed_summary.get("count") != len(normalized)
            or changed_summary.get("sha256") != expected_changed_digest
        ):
            raise ReviewValidationError("repair candidate changed paths differ")
        evidence_digest = hashlib.sha256(
            _REPAIR_CANDIDATE_DOMAIN
            + candidate_receipt_json.encode("utf-8")
            + b"\0"
            + changed_paths_json.encode("utf-8")
        ).hexdigest()
        owner_id = _require_text(owner_id, "owner_id", maximum=256)
        fencing_token = _require_nonnegative_int(fencing_token, "fencing_token")
        operation_id = _require_text(operation_id, "operation_id", maximum=256)

        def write(connection: sqlite3.Connection) -> StoredRepairCandidate:
            job = self._require_lease(
                connection,
                job_id=job_id,
                owner_id=owner_id,
                fencing_token=fencing_token,
            )
            existing = connection.execute(
                "SELECT * FROM review_repair_candidates "
                "WHERE job_id=? AND prior_generation=? "
                "AND manifest_slice_id=? AND repair_attempt=?",
                (
                    job_id, prior_generation, manifest_slice_id,
                    repair_attempt,
                ),
            ).fetchone()
            expected = (
                repair_attempt,
                prior_target_digest,
                base_integration_oid,
                attempt_plan_id,
                candidate_receipt_json,
                changed_paths_json,
                evidence_digest,
                operation_id,
                fencing_token,
            )
            if existing is not None:
                actual = tuple(
                    existing[name]
                    for name in (
                        "repair_attempt",
                        "prior_target_digest",
                        "base_integration_oid",
                        "attempt_plan_id",
                        "candidate_receipt_json",
                        "changed_paths_json",
                        "evidence_digest",
                        "operation_id",
                        "fencing_token",
                    )
                )
                if actual != expected:
                    raise ReviewStoreConflict(
                        "repair candidate conflicts with immutable evidence"
                    )
                return self._repair_candidate_from_row(existing)
            blocked = connection.execute(
                "SELECT 1 FROM review_blocked_receipts "
                "WHERE job_id=? AND generation=? AND target_digest=? "
                "UNION ALL SELECT 1 FROM review_host_check_failures "
                "WHERE job_id=? AND generation=? AND target_digest=? LIMIT 1",
                (
                    job_id,
                    prior_generation,
                    prior_target_digest,
                    job_id,
                    prior_generation,
                    prior_target_digest,
                ),
            ).fetchone()
            started_rows = connection.execute(
                "SELECT payload_json FROM review_store_events "
                "WHERE job_id=? AND generation=? AND kind='repair_attempt_started'",
                (job_id, prior_generation),
            ).fetchall()
            started = any(
                isinstance(payload, dict)
                and payload.get("manifest_slice_id") == manifest_slice_id
                and payload.get("repair_attempt") == repair_attempt
                for payload in (
                    json.loads(str(row["payload_json"])) for row in started_rows
                )
            )
            try:
                adapter_state = json.loads(str(job["adapter_state_json"]))
            except (TypeError, json.JSONDecodeError):
                adapter_state = None
            if (
                job["state"] != "blocked"
                or job["current_generation"] != prior_generation
                or job["target_digest"] != prior_target_digest
                or job["integration_oid"] != base_integration_oid
                or blocked is None
                or not started
                or not isinstance(adapter_state, dict)
                or adapter_state.get("contract_digest")
                != receipt.get("promotion_contract_digest")
            ):
                raise ReviewStoreConflict("repair candidate target is stale")
            connection.execute(
                """
                INSERT INTO review_repair_candidates (
                    job_id, prior_generation, manifest_slice_id, repair_attempt,
                    prior_target_digest, base_integration_oid, attempt_plan_id,
                    candidate_receipt_json, changed_paths_json, evidence_digest,
                    operation_id, fencing_token
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    prior_generation,
                    manifest_slice_id,
                    repair_attempt,
                    prior_target_digest,
                    base_integration_oid,
                    attempt_plan_id,
                    candidate_receipt_json,
                    changed_paths_json,
                    evidence_digest,
                    operation_id,
                    fencing_token,
                ),
            )
            self._append_event_conn(
                connection,
                job_id=job_id,
                generation=prior_generation,
                owner_id=owner_id,
                fencing_token=fencing_token,
                operation_id=operation_id,
                kind="repair_candidate_frozen",
                target_digest=prior_target_digest,
                payload={
                    "evidence_digest": evidence_digest,
                    "manifest_slice_id": manifest_slice_id,
                    "repair_attempt": repair_attempt,
                },
            )
            stored = connection.execute(
                "SELECT * FROM review_repair_candidates "
                "WHERE job_id=? AND prior_generation=? "
                "AND manifest_slice_id=? AND repair_attempt=?",
                (
                    job_id, prior_generation, manifest_slice_id,
                    repair_attempt,
                ),
            ).fetchone()
            if stored is None:
                raise ReviewStoreConflict("repair candidate was not stored")
            return self._repair_candidate_from_row(stored)

        return self._write(write)  # type: ignore[return-value]

    def list_repair_candidates(
        self, job_id: str, *, prior_generation: int,
    ) -> tuple[StoredRepairCandidate, ...]:
        """Read immutable per-slice repair results for restart adoption."""

        job_id = _require_text(job_id, "job_id", maximum=256)
        prior_generation = _require_nonnegative_int(
            prior_generation, "prior_generation"
        )
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM review_repair_candidates "
                "WHERE job_id=? AND prior_generation=? "
                "ORDER BY manifest_slice_id, repair_attempt",
                (job_id, prior_generation),
            ).fetchall()
        candidates = tuple(self._repair_candidate_from_row(row) for row in rows)
        events = {event.operation_id: event for event in self.list_events(job_id)}
        for candidate in candidates:
            try:
                receipt = json.loads(candidate.candidate_receipt_json)
                paths = json.loads(candidate.changed_paths_json)
                event = events[candidate.operation_id]
                event_payload = json.loads(event.payload_json)
            except (KeyError, TypeError, json.JSONDecodeError):
                raise ReviewStoreConflict(
                    "durable repair candidate is invalid"
                ) from None
            evidence_digest = hashlib.sha256(
                _REPAIR_CANDIDATE_DOMAIN
                + _canonical_json(receipt).encode("utf-8")
                + b"\0"
                + _canonical_json(paths).encode("utf-8")
            ).hexdigest()
            if (
                _canonical_json(receipt) != candidate.candidate_receipt_json
                or _canonical_json(paths) != candidate.changed_paths_json
                or evidence_digest != candidate.evidence_digest
                or event.kind != "repair_candidate_frozen"
                or event.target_digest != candidate.prior_target_digest
                or event_payload != {
                    "evidence_digest": candidate.evidence_digest,
                    "manifest_slice_id": candidate.manifest_slice_id,
                    "repair_attempt": candidate.repair_attempt,
                }
            ):
                raise ReviewStoreConflict(
                    "durable repair candidate evidence differs"
                )
        return candidates

    def record_repair_frozen(
        self,
        *,
        job_id: str,
        prior_generation: int,
        generation: int,
        prior_target_digest: str,
        integration_oid: str,
        integration_tree_oid: str,
        integration_ref: str,
        integration_receipt_digest: str,
        candidate_receipts_json: str,
        owner_id: str,
        fencing_token: int,
        operation_id: str,
    ) -> StoredRepairCheckpoint:
        """Store one frozen repair integration before checks start."""

        job_id = _require_text(job_id, "job_id", maximum=256)
        prior_generation = _require_nonnegative_int(
            prior_generation, "prior_generation"
        )
        generation = _require_nonnegative_int(generation, "generation")
        if generation != prior_generation + 1:
            raise ReviewValidationError("repair generation must be consecutive")
        prior_target_digest = _require_digest(
            prior_target_digest, "prior_target_digest"
        )
        integration_oid = _require_oid(integration_oid, "integration_oid")
        integration_tree_oid = _require_oid(
            integration_tree_oid, "integration_tree_oid"
        )
        integration_ref = _require_text(
            integration_ref, "integration_ref", maximum=1024
        )
        if not integration_ref.startswith("refs/hermes-bestplan-integrations/"):
            raise ReviewValidationError("repair integration ref is not BestPlan-owned")
        integration_receipt_digest = _require_digest(
            integration_receipt_digest, "integration_receipt_digest"
        )
        candidate_receipts_json = _bounded_canonical_json(
            candidate_receipts_json,
            "candidate_receipts_json",
            expected_type=list,
        )
        if not json.loads(candidate_receipts_json):
            raise ReviewValidationError("repair candidates must not be empty")
        candidate_receipts_digest = hashlib.sha256(
            b"hermes.bestplan.repair-candidates.v1\0"
            + candidate_receipts_json.encode("utf-8")
        ).hexdigest()
        owner_id = _require_text(owner_id, "owner_id", maximum=256)
        fencing_token = _require_nonnegative_int(fencing_token, "fencing_token")
        operation_id = _require_text(operation_id, "operation_id", maximum=256)

        def write(connection: sqlite3.Connection) -> StoredRepairCheckpoint:
            job = self._require_lease(
                connection,
                job_id=job_id,
                owner_id=owner_id,
                fencing_token=fencing_token,
            )
            existing = connection.execute(
                """
                SELECT * FROM review_repair_checkpoints
                WHERE job_id=? AND generation=?
                """,
                (job_id, generation),
            ).fetchone()
            expected = (
                prior_generation,
                prior_target_digest,
                integration_oid,
                integration_tree_oid,
                integration_ref,
                integration_receipt_digest,
                candidate_receipts_json,
                candidate_receipts_digest,
                operation_id,
                fencing_token,
            )
            if existing is not None:
                actual = tuple(
                    existing[name]
                    for name in (
                        "prior_generation",
                        "prior_target_digest",
                        "integration_oid",
                        "integration_tree_oid",
                        "integration_ref",
                        "integration_receipt_digest",
                        "candidate_receipts_json",
                        "candidate_receipts_digest",
                        "operation_id",
                        "fencing_token",
                    )
                )
                if actual != expected:
                    raise ReviewStoreConflict(
                        "repair checkpoint conflicts with immutable evidence"
                    )
                return self._repair_from_row(existing)
            blocked = connection.execute(
                """
                SELECT * FROM review_blocked_receipts
                WHERE job_id=? AND generation=?
                """,
                (job_id, prior_generation),
            ).fetchone()
            host_check_failure = connection.execute(
                """
                SELECT * FROM review_host_check_failures
                WHERE job_id=? AND generation=?
                """,
                (job_id, prior_generation),
            ).fetchone()
            current_blocker = (
                blocked if blocked is not None else host_check_failure
            )
            if (
                job["state"] != "blocked"
                or job["current_generation"] != prior_generation
                or job["target_digest"] != prior_target_digest
                or current_blocker is None
                or current_blocker["target_digest"] != prior_target_digest
            ):
                raise ReviewStoreConflict("repair checkpoint has no current blocker")
            connection.execute(
                """
                INSERT INTO review_repair_checkpoints (
                    job_id, prior_generation, generation, prior_target_digest,
                    integration_oid, integration_tree_oid, integration_ref,
                    integration_receipt_digest, candidate_receipts_json,
                    candidate_receipts_digest, operation_id, fencing_token
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, prior_generation, generation, *expected[1:]),
            )
            if connection.execute(
                """
                UPDATE review_jobs SET state='checking'
                WHERE job_id=? AND current_generation=? AND state='blocked'
                  AND owner_id=? AND fencing_token=? AND cancel_requested=0
                """,
                (job_id, prior_generation, owner_id, fencing_token),
            ).rowcount != 1:
                raise ReviewLeaseConflict("repair checkpoint lost its owner lease")
            self._append_event_conn(
                connection,
                job_id=job_id,
                generation=generation,
                owner_id=owner_id,
                fencing_token=fencing_token,
                operation_id=operation_id,
                kind="repair_frozen",
                target_digest=prior_target_digest,
                payload={
                    "candidate_receipts_digest": candidate_receipts_digest,
                    "integration_oid": integration_oid,
                    "integration_receipt_digest": integration_receipt_digest,
                },
            )
            stored = connection.execute(
                """
                SELECT * FROM review_repair_checkpoints
                WHERE job_id=? AND generation=?
                """,
                (job_id, generation),
            ).fetchone()
            if stored is None:
                raise ReviewStoreConflict("repair checkpoint was not stored")
            return self._repair_from_row(stored)

        return self._write(write)  # type: ignore[return-value]

    def record_checks_passed(
        self,
        *,
        job_id: str,
        generation: int,
        target: ReviewTarget,
        artifact: ReviewArtifact | None = None,
        check_receipt_json: str,
        owner_id: str,
        fencing_token: int,
        operation_id: str,
    ) -> StoredCheckCheckpoint:
        """Store fresh exact checks and open the repaired review generation."""

        job_id = _require_text(job_id, "job_id", maximum=256)
        generation = _require_nonnegative_int(generation, "generation")
        if not isinstance(target, ReviewTarget) or target.generation != generation:
            raise ReviewValidationError("check checkpoint target is invalid")
        if artifact is not None and (
            not isinstance(artifact, ReviewArtifact)
            or artifact.target_digest != target.target_digest
        ):
            raise ReviewValidationError("check checkpoint artifact is invalid")
        check_receipt_json = _bounded_canonical_json(
            check_receipt_json,
            "check_receipt_json",
            expected_type=dict,
        )
        check_receipt_json_digest = hashlib.sha256(
            b"hermes.bestplan.check-checkpoint.v1\0"
            + check_receipt_json.encode("utf-8")
        ).hexdigest()
        owner_id = _require_text(owner_id, "owner_id", maximum=256)
        fencing_token = _require_nonnegative_int(fencing_token, "fencing_token")
        operation_id = _require_text(operation_id, "operation_id", maximum=256)

        def write(connection: sqlite3.Connection) -> StoredCheckCheckpoint:
            job = self._require_lease(
                connection,
                job_id=job_id,
                owner_id=owner_id,
                fencing_token=fencing_token,
            )
            existing = connection.execute(
                """
                SELECT * FROM review_check_checkpoints
                WHERE job_id=? AND generation=?
                """,
                (job_id, generation),
            ).fetchone()
            expected = (
                target.target_digest,
                target.integration_oid,
                target.check_receipt_digest,
                target.canonical_json,
                check_receipt_json,
                check_receipt_json_digest,
                operation_id,
                fencing_token,
            )
            if existing is not None:
                actual = tuple(
                    existing[name]
                    for name in (
                        "target_digest",
                        "integration_oid",
                        "check_receipt_digest",
                        "target_json",
                        "check_receipt_json",
                        "check_receipt_json_digest",
                        "operation_id",
                        "fencing_token",
                    )
                )
                if actual != expected:
                    raise ReviewStoreConflict(
                        "check checkpoint conflicts with immutable evidence"
                    )
                return self._check_from_row(existing)
            repair = connection.execute(
                """
                SELECT * FROM review_repair_checkpoints
                WHERE job_id=? AND generation=?
                """,
                (job_id, generation),
            ).fetchone()
            if (
                job["state"] != "checking"
                or job["current_generation"] != generation - 1
                or job["source_kind"] != target.source_kind
                or job["source_id"] != target.plan_id
                or job["policy_digest"] != target.policy_digest
                or repair is None
                or repair["integration_oid"] != target.integration_oid
                or repair["integration_tree_oid"] != target.integration_tree_oid
                or repair["integration_ref"] != target.integration_ref
                or repair["integration_receipt_digest"]
                != target.integration_receipt_digest
            ):
                raise ReviewStoreConflict("check checkpoint target is stale")
            connection.execute(
                """
                INSERT INTO review_generations (
                    job_id, generation, state, target_digest, integration_oid,
                    check_receipt_digest, target_json, artifact_json
                ) VALUES (?, ?, 'reviewing', ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    generation,
                    target.target_digest,
                    target.integration_oid,
                    target.check_receipt_digest,
                    target.canonical_json,
                    None if artifact is None else artifact.canonical_json,
                ),
            )
            connection.execute(
                """
                INSERT INTO review_check_checkpoints (
                    job_id, generation, target_digest, integration_oid,
                    check_receipt_digest, target_json, check_receipt_json,
                    check_receipt_json_digest, operation_id, fencing_token
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, generation, *expected),
            )
            if connection.execute(
                """
                UPDATE review_jobs
                SET state='reviewing', current_generation=?, target_digest=?,
                    integration_oid=?, check_receipt_digest=?
                WHERE job_id=? AND current_generation=? AND state='checking'
                  AND owner_id=? AND fencing_token=? AND cancel_requested=0
                """,
                (
                    generation,
                    target.target_digest,
                    target.integration_oid,
                    target.check_receipt_digest,
                    job_id,
                    generation - 1,
                    owner_id,
                    fencing_token,
                ),
            ).rowcount != 1:
                raise ReviewLeaseConflict("check checkpoint lost its owner lease")
            self._append_event_conn(
                connection,
                job_id=job_id,
                generation=generation,
                owner_id=owner_id,
                fencing_token=fencing_token,
                operation_id=operation_id,
                kind="checks_passed",
                target_digest=target.target_digest,
                payload={
                    "check_receipt_digest": target.check_receipt_digest,
                    "check_receipt_json_digest": check_receipt_json_digest,
                    "integration_oid": target.integration_oid,
                },
            )
            stored = connection.execute(
                """
                SELECT * FROM review_check_checkpoints
                WHERE job_id=? AND generation=?
                """,
                (job_id, generation),
            ).fetchone()
            if stored is None:
                raise ReviewStoreConflict("check checkpoint was not stored")
            return self._check_from_row(stored)

        return self._write(write)  # type: ignore[return-value]

    def record_generation_pass(
        self,
        *,
        job_id: str,
        generation: int,
        target_digest: str,
        integration_oid: str,
        check_receipt_digest: str,
        review_receipt_digest: str,
        owner_id: str,
        fencing_token: int,
        operation_id: str,
    ) -> StoredReviewPass:
        """Commit a pass only from both exact, blocker-free reviewer slots."""

        job_id = _require_text(job_id, "job_id", maximum=256)
        generation = _require_nonnegative_int(generation, "generation")
        target_digest = _require_digest(target_digest, "target_digest")
        integration_oid = _require_oid(integration_oid, "integration_oid")
        check_receipt_digest = _require_digest(
            check_receipt_digest, "check_receipt_digest"
        )
        review_receipt_digest = _require_digest(
            review_receipt_digest, "review_receipt_digest"
        )
        owner_id = _require_text(owner_id, "owner_id", maximum=256)
        fencing_token = _require_nonnegative_int(fencing_token, "fencing_token")
        operation_id = _require_text(operation_id, "operation_id", maximum=256)

        def write(connection: sqlite3.Connection) -> StoredReviewPass:
            job = self._require_lease(
                connection,
                job_id=job_id,
                owner_id=owner_id,
                fencing_token=fencing_token,
            )
            existing = connection.execute(
                """
                SELECT * FROM review_pass_receipts
                WHERE job_id=? AND generation=?
                """,
                (job_id, generation),
            ).fetchone()
            if existing is not None:
                stored = self._pass_from_row(existing)
                expected = (
                    target_digest,
                    integration_oid,
                    check_receipt_digest,
                    review_receipt_digest,
                    operation_id,
                    fencing_token,
                )
                actual = (
                    stored.target_digest,
                    stored.integration_oid,
                    stored.check_receipt_digest,
                    stored.review_receipt_digest,
                    stored.operation_id,
                    stored.fencing_token,
                )
                if actual != expected:
                    raise ReviewStoreConflict(
                        "review pass conflicts with immutable receipt evidence"
                    )
                return stored
            generation_row = connection.execute(
                """
                SELECT * FROM review_generations
                WHERE job_id=? AND generation=?
                """,
                (job_id, generation),
            ).fetchone()
            if (
                job["state"] != "reviewing"
                or job["current_generation"] != generation
                or job["target_digest"] != target_digest
                or job["integration_oid"] != integration_oid
                or job["check_receipt_digest"] != check_receipt_digest
                or generation_row is None
                or generation_row["state"] != "reviewing"
                or generation_row["target_digest"] != target_digest
                or generation_row["integration_oid"] != integration_oid
                or generation_row["check_receipt_digest"]
                != check_receipt_digest
            ):
                raise ReviewStoreConflict("review pass target is not current")
            rows = connection.execute(
                """
                SELECT * FROM review_reviewer_receipts
                WHERE job_id=? AND generation=? ORDER BY slot
                """,
                (job_id, generation),
            ).fetchall()
            by_slot = {str(row["slot"]): row for row in rows}
            if (
                set(by_slot) != set(_REQUIRED_SLOTS)
                or len(rows) != len(_REQUIRED_SLOTS)
                or any(
                    not bool(row["passed"])
                    or row["target_digest"] != target_digest
                    or row["integration_oid"] != integration_oid
                    for row in rows
                )
            ):
                raise ReviewStoreConflict(
                    "review pass requires both exact passing reviewer slots"
                )
            slot_payload = [
                {
                    "fencing_token": int(by_slot[slot]["fencing_token"]),
                    "operation_id": str(by_slot[slot]["operation_id"]),
                    "output_digest": str(by_slot[slot]["output_digest"]),
                    "slot": slot,
                    "target_digest": str(by_slot[slot]["target_digest"]),
                    "verdict_digest": str(by_slot[slot]["verdict_digest"]),
                }
                for slot in _REQUIRED_SLOTS
            ]
            slot_json = _canonical_json(slot_payload)
            slot_digest = _domain_digest(_PASS_SLOTS_DOMAIN, slot_json)
            connection.execute(
                """
                INSERT INTO review_pass_receipts (
                    job_id, generation, target_digest, integration_oid,
                    check_receipt_digest, review_receipt_digest,
                    slot_receipts_json, slot_receipts_digest, operation_id,
                    fencing_token
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    generation,
                    target_digest,
                    integration_oid,
                    check_receipt_digest,
                    review_receipt_digest,
                    slot_json,
                    slot_digest,
                    operation_id,
                    fencing_token,
                ),
            )
            if connection.execute(
                """
                UPDATE review_generations SET state='passed'
                WHERE job_id=? AND generation=? AND state='reviewing'
                """,
                (job_id, generation),
            ).rowcount != 1:
                raise ReviewStoreConflict("review generation pass lost its state")
            if connection.execute(
                """
                UPDATE review_jobs SET state='passed'
                WHERE job_id=? AND current_generation=? AND state='reviewing'
                  AND owner_id=? AND fencing_token=? AND cancel_requested=0
                """,
                (job_id, generation, owner_id, fencing_token),
            ).rowcount != 1:
                raise ReviewLeaseConflict("review pass lost its owner lease")
            self._append_event_conn(
                connection,
                job_id=job_id,
                generation=generation,
                owner_id=owner_id,
                fencing_token=fencing_token,
                operation_id=operation_id,
                kind="review_pass",
                target_digest=target_digest,
                payload={
                    "check_receipt_digest": check_receipt_digest,
                    "integration_oid": integration_oid,
                    "review_receipt_digest": review_receipt_digest,
                    "slot_receipts_digest": slot_digest,
                },
            )
            stored = connection.execute(
                """
                SELECT * FROM review_pass_receipts
                WHERE job_id=? AND generation=?
                """,
                (job_id, generation),
            ).fetchone()
            if stored is None:
                raise ReviewStoreConflict("review pass receipt was not stored")
            return self._pass_from_row(stored)

        return self._write(write)  # type: ignore[return-value]

    def latest_exact_pass(
        self,
        *,
        target: ReviewTarget,
        review_receipt_digest: str,
    ) -> StoredReviewPass | None:
        if not isinstance(target, ReviewTarget):
            raise ReviewValidationError("review pass target is invalid")
        review_receipt_digest = _require_digest(
            review_receipt_digest, "review_receipt_digest"
        )
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT pass.* FROM review_pass_receipts AS pass
                JOIN review_jobs AS job ON job.job_id=pass.job_id
                JOIN review_generations AS generation
                  ON generation.job_id=pass.job_id
                 AND generation.generation=pass.generation
                WHERE job.source_kind=? AND job.source_id=?
                  AND job.state='passed' AND job.cancel_requested=0
                  AND job.current_generation=?
                  AND pass.generation=? AND pass.target_digest=?
                  AND pass.integration_oid=?
                  AND pass.check_receipt_digest=?
                  AND pass.review_receipt_digest=?
                  AND pass.fencing_token=job.fencing_token
                  AND generation.target_json=? AND generation.state='passed'
                """,
                (
                    target.source_kind,
                    target.plan_id,
                    target.generation,
                    target.generation,
                    target.target_digest,
                    target.integration_oid,
                    target.check_receipt_digest,
                    review_receipt_digest,
                    target.canonical_json,
                ),
            ).fetchall()
        return self._pass_from_row(rows[0]) if len(rows) == 1 else None

    @classmethod
    def latest_exact_pass_in_transaction(
        cls,
        connection: sqlite3.Connection,
        *,
        target: ReviewTarget,
        review_receipt_digest: str,
    ) -> StoredReviewPass:
        """Read one current exact pass from the caller's open transaction."""

        if not isinstance(target, ReviewTarget):
            raise ReviewValidationError("review pass target is invalid")
        review_receipt_digest = _require_digest(
            review_receipt_digest, "review_receipt_digest"
        )
        rows = connection.execute(
            """
            SELECT pass.* FROM review_pass_receipts AS pass
            JOIN review_jobs AS job ON job.job_id=pass.job_id
            JOIN review_generations AS generation
              ON generation.job_id=pass.job_id
             AND generation.generation=pass.generation
            WHERE job.source_kind=? AND job.source_id=?
              AND job.state='passed' AND job.cancel_requested=0
              AND job.current_generation=?
              AND pass.generation=? AND pass.target_digest=?
              AND pass.integration_oid=? AND pass.check_receipt_digest=?
              AND pass.review_receipt_digest=?
              AND generation.target_json=? AND generation.state='passed'
            """,
            (
                target.source_kind,
                target.plan_id,
                target.generation,
                target.generation,
                target.target_digest,
                target.integration_oid,
                target.check_receipt_digest,
                review_receipt_digest,
                target.canonical_json,
            ),
        ).fetchall()
        if len(rows) != 1:
            raise ReviewStoreConflict("exact review pass is missing or ambiguous")
        return cls._pass_from_row(rows[0])

    @classmethod
    def consume_latest_pass_in_transaction(
        cls,
        connection: sqlite3.Connection,
        *,
        target: ReviewTarget,
        review_receipt_digest: str,
        consumer_plan_id: str,
    ) -> ReviewPassConsumption:
        """Consume the latest exact pass in the caller's open write transaction."""

        if not isinstance(target, ReviewTarget):
            raise ReviewValidationError("review pass target is invalid")
        review_receipt_digest = _require_digest(
            review_receipt_digest, "review_receipt_digest"
        )
        consumer_plan_id = _require_text(
            consumer_plan_id, "review pass consumer", maximum=256
        )
        rows = connection.execute(
            """
            SELECT pass.*, job.state AS job_state, job.cancel_requested,
                   job.current_generation, job.owner_id AS job_owner_id,
                   job.fencing_token AS job_fencing_token,
                   generation.state AS generation_state,
                   generation.target_json
            FROM review_pass_receipts AS pass
            JOIN review_jobs AS job ON job.job_id=pass.job_id
            JOIN review_generations AS generation
              ON generation.job_id=pass.job_id
             AND generation.generation=pass.generation
            WHERE job.source_kind=? AND job.source_id=?
              AND pass.generation=? AND pass.target_digest=?
              AND pass.integration_oid=? AND pass.check_receipt_digest=?
              AND pass.review_receipt_digest=?
            """,
            (
                target.source_kind,
                target.plan_id,
                target.generation,
                target.target_digest,
                target.integration_oid,
                target.check_receipt_digest,
                review_receipt_digest,
            ),
        ).fetchall()
        if len(rows) != 1:
            raise ReviewStoreConflict("exact review pass is missing or ambiguous")
        row = rows[0]
        if (
            row["job_state"] != "passed"
            or bool(row["cancel_requested"])
            or int(row["current_generation"]) != target.generation
            or row["generation_state"] != "passed"
            or row["target_json"] != target.canonical_json
            or not isinstance(row["job_owner_id"], str)
            or not row["job_owner_id"]
        ):
            raise ReviewStoreConflict("review pass is stale or not current")
        existing = connection.execute(
            "SELECT * FROM review_pass_consumptions WHERE job_id=?",
            (row["job_id"],),
        ).fetchone()
        expected = (
            int(row["generation"]),
            consumer_plan_id,
            target.target_digest,
            review_receipt_digest,
            int(row["job_fencing_token"]),
        )
        if existing is not None:
            current = (
                int(existing["generation"]),
                str(existing["consumer_plan_id"]),
                str(existing["target_digest"]),
                str(existing["review_receipt_digest"]),
                int(existing["fencing_token"]),
            )
            if current != expected:
                raise ReviewStoreConflict(
                    "review pass was consumed by a different landing"
                )
            return cls._consumption_from_row(existing)
        connection.execute(
            """
            INSERT INTO review_pass_consumptions (
                job_id, generation, consumer_plan_id, target_digest,
                review_receipt_digest, fencing_token
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (row["job_id"], *expected),
        )
        changed = connection.execute(
            """
            UPDATE review_jobs
            SET state='landing_prepared', prepared_consumer_plan_id=?,
                prepared_target_digest=?, prepared_review_receipt_digest=?
            WHERE job_id=? AND state='passed' AND cancel_requested=0
              AND current_generation=? AND target_digest=?
              AND integration_oid=? AND check_receipt_digest=?
            """,
            (
                consumer_plan_id,
                target.target_digest,
                review_receipt_digest,
                row["job_id"],
                target.generation,
                target.target_digest,
                target.integration_oid,
                target.check_receipt_digest,
            ),
        ).rowcount
        if changed != 1:
            raise ReviewStoreConflict("review pass lost the landing-prepare race")
        cls._append_event_conn(
            connection,
            job_id=str(row["job_id"]),
            generation=target.generation,
            owner_id=str(row["job_owner_id"]),
            fencing_token=int(row["job_fencing_token"]),
            operation_id=f"landing-prepare:{consumer_plan_id}",
            kind="landing_prepared",
            target_digest=target.target_digest,
            payload={
                "consumer_plan_id": consumer_plan_id,
                "review_receipt_digest": review_receipt_digest,
            },
        )
        stored = connection.execute(
            "SELECT * FROM review_pass_consumptions WHERE job_id=?",
            (row["job_id"],),
        ).fetchone()
        if stored is None:
            raise ReviewStoreConflict("review pass consumption was not stored")
        return cls._consumption_from_row(stored)

    def request_cancel(
        self,
        *,
        job_id: str,
        owner_id: str,
        fencing_token: int,
        operation_id: str,
        signal_children: Callable[[], object],
    ) -> ReviewJob:
        job_id = _require_text(job_id, "job_id", maximum=256)
        owner_id = _require_text(owner_id, "owner_id", maximum=256)
        fencing_token = _require_nonnegative_int(fencing_token, "fencing_token")
        operation_id = _require_text(operation_id, "operation_id", maximum=256)
        if not callable(signal_children):
            raise ReviewValidationError("cancel signal must be callable")

        def write(connection: sqlite3.Connection) -> ReviewJob:
            row = self._job_row(connection, job_id)
            if (
                row["owner_id"] != owner_id
                or int(row["fencing_token"]) != fencing_token
                or row["lease_expires_at_ns"] is None
            ):
                raise ReviewLeaseConflict("review mutation has a stale owner lease")
            if row["state"] == "landing_claimed":
                raise ReviewLeaseConflict("landing_already_claimed")
            generation = (
                0 if row["current_generation"] is None else int(row["current_generation"])
            )
            changed = connection.execute(
                """
                UPDATE review_jobs
                SET state = 'cancel_requested', cancel_requested = 1
                WHERE job_id = ? AND owner_id = ? AND fencing_token = ?
                """,
                (job_id, owner_id, fencing_token),
            ).rowcount
            if changed != 1:
                raise ReviewLeaseConflict("review cancellation lost its owner lease")
            self._append_event_conn(
                connection,
                job_id=job_id,
                generation=generation,
                owner_id=owner_id,
                fencing_token=fencing_token,
                operation_id=operation_id,
                kind="cancel_requested",
                target_digest=str(row["target_digest"]),
                payload={"cancel_requested": True},
            )
            return self._job_from_row(self._job_row(connection, job_id))

        durable = self._write(write)
        signal_children()
        return durable  # type: ignore[return-value]

    def request_manual_cancel_intent(
        self,
        *,
        job_id: str,
        expected_target_digest: str,
        operation_id: str,
        signal_children: Callable[[], object],
    ) -> ReviewJob:
        """Persist an operator stop against one immutable manual review job.

        A review owner lease can expire while SQLite is temporarily
        unavailable.  Cancellation therefore fences the immutable job target,
        not the lease that happened to own it when the operator pressed stop.
        The transaction records the current owner token and sets the durable
        cancel bit before any process-local child is signalled.
        """

        job_id = _require_text(job_id, "job_id", maximum=256)
        expected_target_digest = _require_digest(
            expected_target_digest, "expected_target_digest"
        )
        operation_id = _require_text(operation_id, "operation_id", maximum=256)
        if not callable(signal_children):
            raise ReviewValidationError("cancel signal must be callable")

        def write(connection: sqlite3.Connection) -> ReviewJob:
            row = self._job_row(connection, job_id)
            if (
                row["source_kind"] != "manual_snapshot"
                or row["target_digest"] != expected_target_digest
            ):
                raise ReviewStoreConflict(
                    "manual cancellation target identity differs"
                )
            if row["state"] in {"landing_claimed", "landed"}:
                raise ReviewLeaseConflict("landing_already_claimed")
            if bool(row["cancel_requested"]) or row["state"] == "cancelled":
                return self._job_from_row(row)
            current_owner = row["owner_id"]
            if not isinstance(current_owner, str) or not current_owner:
                raise ReviewLeaseConflict(
                    "manual cancellation has no durable owner fence"
                )
            current_token = int(row["fencing_token"])
            generation = (
                0 if row["current_generation"] is None
                else int(row["current_generation"])
            )
            changed = connection.execute(
                """
                UPDATE review_jobs
                SET state='cancel_requested', cancel_requested=1
                WHERE job_id=? AND target_digest=? AND cancel_requested=0
                  AND state NOT IN ('landing_claimed', 'landed', 'cancelled')
                """,
                (job_id, expected_target_digest),
            ).rowcount
            if changed != 1:
                raise ReviewStoreConflict(
                    "manual cancellation lost its immutable job fence"
                )
            self._append_event_conn(
                connection,
                job_id=job_id,
                generation=generation,
                owner_id=current_owner,
                fencing_token=current_token,
                operation_id=operation_id,
                kind="cancel_requested",
                target_digest=expected_target_digest,
                payload={"cancel_requested": True},
            )
            return self._job_from_row(self._job_row(connection, job_id))

        durable = self._write(write)
        signal_children()
        return durable  # type: ignore[return-value]

    def finalize_cancel(
        self,
        *,
        job_id: str,
        owner_id: str,
        fencing_token: int,
        operation_id: str,
    ) -> ReviewJob:
        """Record child extinction after durable cancellation was requested."""

        job_id = _require_text(job_id, "job_id", maximum=256)
        owner_id = _require_text(owner_id, "owner_id", maximum=256)
        fencing_token = _require_nonnegative_int(fencing_token, "fencing_token")
        operation_id = _require_text(operation_id, "operation_id", maximum=256)

        def write(connection: sqlite3.Connection) -> ReviewJob:
            row = self._require_lease(
                connection,
                job_id=job_id,
                owner_id=owner_id,
                fencing_token=fencing_token,
                allow_cancel_requested=True,
                allow_expired=True,
            )
            if not bool(row["cancel_requested"]):
                raise ReviewStoreConflict("review cancellation was not requested")
            if row["state"] == "cancelled":
                return self._job_from_row(row)
            generation = (
                0 if row["current_generation"] is None else int(row["current_generation"])
            )
            changed = connection.execute(
                """
                UPDATE review_jobs SET state='cancelled'
                WHERE job_id=? AND owner_id=? AND fencing_token=?
                  AND cancel_requested=1
                """,
                (job_id, owner_id, fencing_token),
            ).rowcount
            if changed != 1:
                raise ReviewLeaseConflict("review cancel finalization lost ownership")
            self._append_event_conn(
                connection,
                job_id=job_id,
                generation=generation,
                owner_id=owner_id,
                fencing_token=fencing_token,
                operation_id=operation_id,
                kind="cancelled",
                target_digest=str(row["target_digest"]),
                payload={"children_extinct": True},
            )
            return self._job_from_row(self._job_row(connection, job_id))

        return self._write(write)  # type: ignore[return-value]

    def resume_job(
        self,
        *,
        job_id: str,
        owner_id: str,
        fencing_token: int,
    ) -> ReviewResume:
        job_id = _require_text(job_id, "job_id", maximum=256)
        owner_id = _require_text(owner_id, "owner_id", maximum=256)
        fencing_token = _require_nonnegative_int(fencing_token, "fencing_token")
        with self._connect() as connection:
            job = self._require_lease(
                connection,
                job_id=job_id,
                owner_id=owner_id,
                fencing_token=fencing_token,
            )
            if job["current_generation"] is None:
                if job["state"] != "queued":
                    raise ReviewStoreConflict(
                        "review job has no durable generation checkpoint"
                    )
                try:
                    adapter_state = json.loads(str(job["adapter_state_json"]))
                except (json.JSONDecodeError, TypeError):
                    adapter_state = {}
                pending_initial_checks = bool(
                    isinstance(adapter_state, dict)
                    and isinstance(
                        adapter_state.get("initial_check_pending"), dict
                    )
                )
                return ReviewResume(
                    job_id=job_id,
                    generation=0,
                    target_digest=str(job["target_digest"]),
                    adopted_reviewer_receipts=(),
                    missing_reviewer_slots=_REQUIRED_SLOTS,
                    next_action=(
                        "initial_checks"
                        if pending_initial_checks
                        else "start_generation"
                    ),
                )
            generation = int(job["current_generation"])
            target = connection.execute(
                """
                SELECT * FROM review_generations
                WHERE job_id = ? AND generation = ?
                """,
                (job_id, generation),
            ).fetchone()
            if (
                target is None
                or target["target_digest"] != job["target_digest"]
                or target["integration_oid"] != job["integration_oid"]
                or target["check_receipt_digest"] != job["check_receipt_digest"]
            ):
                raise ReviewStoreConflict("durable review generation is stale")
            rows = connection.execute(
                """
                SELECT * FROM review_reviewer_receipts
                WHERE job_id = ? AND generation = ?
                """,
                (job_id, generation),
            ).fetchall()
            blocked_row = connection.execute(
                """
                SELECT * FROM review_blocked_receipts
                WHERE job_id=? AND generation=?
                """,
                (job_id, generation),
            ).fetchone()
            host_check_failure_row = connection.execute(
                """
                SELECT * FROM review_host_check_failures
                WHERE job_id=? AND generation=?
                """,
                (job_id, generation),
            ).fetchone()
            repair_row = connection.execute(
                """
                SELECT * FROM review_repair_checkpoints
                WHERE job_id=? ORDER BY generation DESC LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            check_row = connection.execute(
                """
                SELECT * FROM review_check_checkpoints
                WHERE job_id=? AND generation=?
                """,
                (job_id, generation),
            ).fetchone()
            manual_checkpoint_rows = connection.execute(
                """
                SELECT * FROM review_manual_checkpoints
                WHERE job_id=? AND generation=?
                ORDER BY CASE phase
                    WHEN 'repair_prepared' THEN 1
                    WHEN 'repair_applied' THEN 2
                    WHEN 'checks_passed' THEN 3
                    ELSE 0 END
                """,
                (job_id, generation + 1),
            ).fetchall()
            pass_row = connection.execute(
                """
                SELECT * FROM review_pass_receipts
                WHERE job_id=? AND generation=?
                """,
                (job_id, generation),
            ).fetchone()
            consumption_row = connection.execute(
                "SELECT * FROM review_pass_consumptions WHERE job_id=?",
                (job_id,),
            ).fetchone()
        by_slot: dict[str, StoredReviewerReceipt] = {}
        for row in rows:
            receipt = self._receipt_from_row(row)
            if (
                receipt.slot not in _REQUIRED_SLOTS
                or receipt.slot in by_slot
                or receipt.target_digest != target["target_digest"]
                or receipt.integration_oid != target["integration_oid"]
            ):
                raise ReviewStoreConflict("durable reviewer receipt is stale")
            by_slot[receipt.slot] = receipt
        adopted = tuple(by_slot[slot] for slot in _REQUIRED_SLOTS if slot in by_slot)
        missing = tuple(slot for slot in _REQUIRED_SLOTS if slot not in by_slot)
        state = str(job["state"])
        blocked = (
            None if blocked_row is None else self._blocked_from_row(blocked_row)
        )
        host_check_failure = (
            None
            if host_check_failure_row is None
            else self._host_check_failure_from_row(host_check_failure_row)
        )
        repair = (
            None if repair_row is None else self._repair_from_row(repair_row)
        )
        check = None if check_row is None else self._check_from_row(check_row)
        review_pass = None if pass_row is None else self._pass_from_row(pass_row)
        manual_checkpoints = {
            str(row["phase"]): self._manual_checkpoint_from_row(row)
            for row in manual_checkpoint_rows
        }
        if state == "reviewing":
            if check is not None and (
                check.target_digest != target["target_digest"]
                or check.integration_oid != target["integration_oid"]
                or check.check_receipt_digest != target["check_receipt_digest"]
                or check.target_json != target["target_json"]
            ):
                raise ReviewStoreConflict("durable check checkpoint is stale")
            next_action = "review_missing_slots"
        elif state == "blocked":
            model_blocked_valid = (
                blocked is not None
                and blocked.target_digest == target["target_digest"]
                and blocked.integration_oid == target["integration_oid"]
                and blocked.check_receipt_digest == target["check_receipt_digest"]
                and not missing
            )
            host_check_valid = (
                host_check_failure is not None
                and host_check_failure.target_digest == target["target_digest"]
                and host_check_failure.integration_oid == target["integration_oid"]
                and not adopted
            )
            if model_blocked_valid == host_check_valid:
                raise ReviewStoreConflict("durable blocker checkpoint is stale")
            next_action = "repair"
        elif state == "checking" or (
            state == "repairing"
            and job["adapter_version"] == _MANUAL_ADAPTER_VERSION
        ):
            if job["adapter_version"] == _MANUAL_ADAPTER_VERSION:
                prepared = manual_checkpoints.get("repair_prepared")
                applied = manual_checkpoints.get("repair_applied")
                passed_checks = manual_checkpoints.get("checks_passed")
                if (
                    prepared is None
                    or prepared.prior_generation != generation
                    or prepared.prior_target_digest != target["target_digest"]
                    or (blocked is None and host_check_failure is None)
                    or (applied is not None and (
                        applied.snapshot_digest != prepared.snapshot_digest
                        or applied.live_state_digest != prepared.live_state_digest
                    ))
                    or (passed_checks is not None and (
                        applied is None
                        or passed_checks.snapshot_digest
                        != applied.snapshot_digest
                        or passed_checks.live_state_digest
                        != applied.live_state_digest
                    ))
                ):
                    raise ReviewStoreConflict(
                        "durable manual checkpoint is stale"
                    )
                next_action = (
                    "manual_begin_generation"
                    if passed_checks is not None
                    else "manual_checks"
                    if applied is not None
                    else "manual_reconcile"
                )
            else:
                if (
                    repair is None
                    or repair.prior_generation != generation
                    or repair.prior_target_digest != target["target_digest"]
                    or repair.generation != generation + 1
                    or (blocked is None and host_check_failure is None)
                ):
                    raise ReviewStoreConflict("durable repair checkpoint is stale")
                next_action = "checks"
        elif state in {"passed", "landing_prepared"}:
            if (
                review_pass is None
                or review_pass.target_digest != target["target_digest"]
                or review_pass.integration_oid != target["integration_oid"]
                or review_pass.check_receipt_digest != target["check_receipt_digest"]
                or missing
                or any(not receipt.passed for receipt in adopted)
            ):
                raise ReviewStoreConflict("durable review pass is stale")
            if state == "landing_prepared":
                if (
                    consumption_row is None
                    or int(consumption_row["generation"]) != generation
                    or consumption_row["consumer_plan_id"] != job["source_id"]
                    or consumption_row["target_digest"] != target["target_digest"]
                    or consumption_row["review_receipt_digest"]
                    != review_pass.review_receipt_digest
                    or job["prepared_consumer_plan_id"] != job["source_id"]
                    or job["prepared_target_digest"] != target["target_digest"]
                    or job["prepared_review_receipt_digest"]
                    != review_pass.review_receipt_digest
                ):
                    raise ReviewStoreConflict(
                        "durable landing preparation is stale"
                    )
            next_action = "handoff_pass"
        elif state == "waiting":
            next_action = "wait_for_host"
        else:
            raise ReviewStoreConflict("durable review state cannot be resumed")
        resume_generation = repair.generation if next_action == "checks" else generation
        return ReviewResume(
            job_id=job_id,
            generation=resume_generation,
            target_digest=str(target["target_digest"]),
            adopted_reviewer_receipts=adopted,
            missing_reviewer_slots=missing,
            next_action=next_action,
            blocking_findings_json=(
                host_check_failure.blocking_findings_json
                if host_check_failure is not None
                else "[]" if blocked is None else blocked.blocking_findings_json
            ),
            review_receipt_digest=(
                review_pass.review_receipt_digest
                if review_pass is not None
                else None if blocked is None else blocked.review_receipt_digest
            ),
            repair_checkpoint=repair,
            check_receipt_json=(
                None if check is None else check.check_receipt_json
            ),
            review_pass=review_pass,
        )

    def wait_for_host(
        self,
        *,
        job_id: str,
        generation: int,
        target_digest: str,
        owner_id: str,
        fencing_token: int,
        operation_id: str,
        reason_code: str,
        payload: Mapping[str, object],
    ) -> ReviewJob:
        """Preserve a resumable job when the current host cannot continue."""

        job_id = _require_text(job_id, "job_id", maximum=256)
        generation = _require_nonnegative_int(generation, "generation")
        target_digest = _require_digest(target_digest, "target_digest")
        owner_id = _require_text(owner_id, "owner_id", maximum=256)
        fencing_token = _require_nonnegative_int(fencing_token, "fencing_token")
        operation_id = _require_text(operation_id, "operation_id", maximum=256)
        reason_code = _require_text(reason_code, "reason_code", maximum=64)
        if reason_code != "blocked_requires_authority":
            raise ReviewValidationError("review wait reason is unsupported")
        if not isinstance(payload, Mapping):
            raise ReviewValidationError("review wait payload is invalid")
        safe_payload = json.loads(_bounded_canonical_json(
            dict(payload), "wait payload", expected_type=dict,
        ))

        def write(connection: sqlite3.Connection) -> ReviewJob:
            job = self._require_lease(
                connection,
                job_id=job_id,
                owner_id=owner_id,
                fencing_token=fencing_token,
            )
            if (
                job["cancel_requested"]
                or job["current_generation"] != generation
                or job["target_digest"] != target_digest
                or job["state"] in {"landing_claimed", "landing_prepared"}
            ):
                raise ReviewStoreConflict("review wait target is stale")
            changed = connection.execute(
                """
                UPDATE review_jobs SET state='waiting'
                WHERE job_id=? AND owner_id=? AND fencing_token=?
                  AND cancel_requested=0
                """,
                (job_id, owner_id, fencing_token),
            ).rowcount
            if changed != 1:
                raise ReviewLeaseConflict("review wait lost its owner lease")
            connection.execute(
                """
                UPDATE review_generations SET state='waiting'
                WHERE job_id=? AND generation=?
                """,
                (job_id, generation),
            )
            self._append_event_conn(
                connection,
                job_id=job_id,
                generation=generation,
                owner_id=owner_id,
                fencing_token=fencing_token,
                operation_id=operation_id,
                kind=reason_code,
                target_digest=target_digest,
                payload=safe_payload,
            )
            return self._job_from_row(self._job_row(connection, job_id))

        return self._write(write)  # type: ignore[return-value]

    def list_events(self, job_id: str) -> tuple[ReviewStoreEvent, ...]:
        job_id = _require_text(job_id, "job_id", maximum=256)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM review_store_events
                WHERE job_id = ? ORDER BY event_seq
                """,
                (job_id,),
            ).fetchall()
        events = tuple(self._event_from_row(row) for row in rows)
        previous: str | None = None
        for expected_seq, event in enumerate(events, start=1):
            if (
                event.event_seq != expected_seq
                or event.previous_event_digest != previous
                or event.fencing_token <= 0
                or _domain_digest(_EVENT_PAYLOAD_DOMAIN, event.payload_json)
                != event.payload_digest
            ):
                raise ReviewStoreConflict("durable review event chain is invalid")
            body = {
                "event_seq": event.event_seq,
                "fencing_token": event.fencing_token,
                "generation": event.generation,
                "job_id": event.job_id,
                "kind": event.kind,
                "operation_id": event.operation_id,
                "owner_id": event.owner_id,
                "payload_digest": event.payload_digest,
                "previous_event_digest": event.previous_event_digest,
                "target_digest": event.target_digest,
            }
            if _domain_digest(_STORE_EVENT_DOMAIN, _canonical_json(body)) != (
                event.event_digest
            ):
                raise ReviewStoreConflict("durable review event digest is invalid")
            previous = event.event_digest
        return events
