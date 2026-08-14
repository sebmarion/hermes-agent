"""Append-only local projections for BestPlan V2 promotion proof.

The SQLite ledger is deliberately a *projection*, not an authority.  Relational
triggers protect mixed-version callers from bypassing the append APIs, while a
fresh injected authority-receipt verifier remains mandatory for terminal V2
completion.  Task 8 supplies that external authority and its protected chain.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from agent.bestplan_contract import validate_execution_contract
from agent.bestplan_redaction import (
    REDACTED_OUTPUT_SCHEMA,
    RedactionError,
    RedactedOutput,
    canonical_raw_bytes,
    redact_output,
    validate_redacted_projection,
)


EVENT_SCHEMA = "hermes.bestplan.proof-event.v1"
CANDIDATE_SCHEMA = "hermes.bestplan.candidate-receipt.v1"
AUTHORITY_RECEIPT_SCHEMA = "hermes.bestplan.authority-receipt.v1"
EVENT_HASH_DOMAIN = b"hermes.bestplan.proof-event.v1\0"
CANDIDATE_DIGEST_DOMAIN = b"hermes.bestplan.candidate-receipt.v1\0"
CANDIDATE_SET_DIGEST_DOMAIN = b"hermes.bestplan.candidate-set.v1\0"
AUTHORITY_RECEIPT_DOMAIN = b"hermes.bestplan.authority-receipt.v1\0"
REDACTED_DIGEST_DOMAIN = b"hermes.bestplan.redacted-output.v1\0"
OPERATION_FINGERPRINT_DOMAIN = b"hermes.bestplan.proof-operation.v1\0"
_ZERO_DIGEST = "0" * 64
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_LABEL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
COMPATIBILITY_ERROR_CODES = frozenset(
    {
        "dispatch_deferred",
        "dispatch_failed",
        "dispatch_unknown",
        "recapture_required",
        "recovered_dead_dispatch_owner",
        "recovered_pre_run_schedule",
    }
)
_CANDIDATE_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "version",
        "plan_id",
        "candidate_id",
        "slice_id",
        "attempt_id",
        "commit_oid",
        "tree_oid",
        "base_oid",
        "approval_digest",
        "contract_digest",
        "source_snapshot_digest",
        "output",
        "created_at_policy",
        "created_at_ns",
    }
)
_AUTHORITY_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "version",
        "kind",
        "plan_id",
        "execution_protocol",
        "authority_epoch",
        "event_seq",
        "event_hash",
        "approval_digest",
        "promotion_contract_digest",
        "source_snapshot_digest",
        "candidate_set_digest",
        "repository_id",
        "base_oid",
        "integration_oid",
        "artifact_digest",
        "local",
        "remote",
        "live",
        "controller",
        "issued_at_ns",
        "expires_at_ns",
    }
)

# Success projection edges are explicit.  Transient operational phases may be
# recorded or skipped, but proven milestones cannot be reordered.
AUTHORITY_PHASE_EDGES = frozenset(
    {
        ("captured", "candidate_ready"),
        ("candidate_ready", "queued"),
        ("candidate_ready", "integrating"),
        ("candidate_ready", "integrated_proven"),
        ("queued", "integrating"),
        ("integrating", "integrated_proven"),
        ("integrated_proven", "testing"),
        ("integrated_proven", "tests_verified"),
        ("testing", "tests_verified"),
        ("tests_verified", "reviewing"),
        ("tests_verified", "review_verified"),
        ("reviewing", "review_verified"),
        ("review_verified", "artifact_frozen"),
        ("artifact_frozen", "main_fast_forwarded"),
        ("main_fast_forwarded", "remote_verified"),
        ("remote_verified", "deploying"),
        ("remote_verified", "live_verified"),
        ("deploying", "live_verified"),
    }
)


class ProofError(RuntimeError):
    """Base class for deterministic proof-ledger failures."""


class ProofValidationError(ProofError):
    """A proof event, projection, or receipt is inconsistent."""


class ProofHeadMismatch(ProofError):
    """The caller's expected stream head is stale."""


class ProofOperationConflict(ProofError):
    """An operation UUID was reused for different canonical input."""


class ProofMigrationError(ProofError):
    """An existing partial schema cannot be extended safely."""


def _canonical_json(value: Any) -> str:
    def validate(item: Any, active: set[int]) -> None:
        if item is None or isinstance(item, (bool, int, str)):
            return
        if isinstance(item, float):
            raise ProofValidationError("canonical proof JSON does not allow floats")
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in active:
                raise ProofValidationError("canonical proof JSON contains a cycle")
            active.add(identity)
            try:
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise ProofValidationError(
                            "canonical proof JSON requires string keys"
                        )
                    validate(child, active)
            finally:
                active.remove(identity)
            return
        if isinstance(item, list):
            identity = id(item)
            if identity in active:
                raise ProofValidationError("canonical proof JSON contains a cycle")
            active.add(identity)
            try:
                for child in item:
                    validate(child, active)
            finally:
                active.remove(identity)
            return
        raise ProofValidationError("canonical proof JSON contains an unsupported type")

    validate(value, set())
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: str, domain: bytes) -> str:
    return hashlib.sha256(domain + value.encode("ascii")).hexdigest()


def _label(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _LABEL_RE.fullmatch(value):
        raise ProofValidationError(f"{name} must be a bounded lowercase label")
    return value


def _nonempty(value: Any, name: str, *, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or "\x00" in value
    ):
        raise ProofValidationError(f"{name} must be a bounded nonempty string")
    return value


def _sha256(value: Any, name: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ProofValidationError(f"{name} must be a lowercase sha256 digest")
    return value


def _oid(value: Any, name: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not _OID_RE.fullmatch(value):
        raise ProofValidationError(f"{name} must be a full lowercase Git object id")
    return value


def _operation_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ProofValidationError("operation_id must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ProofValidationError("operation_id must be a canonical UUID") from exc
    if str(parsed) != value:
        raise ProofValidationError("operation_id must be a canonical UUID")
    return value


def _int(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ProofValidationError(f"{name} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True)
class ProofEventReceipt:
    plan_id: str
    stream: str
    authority_epoch: str
    event_seq: int
    event_hash: str
    previous_hash: str | None
    operation_id: str
    protocol: int
    kind: str
    previous_phase: str
    phase: str
    projected_state: str
    approval_digest: str
    contract_digest: str
    source_snapshot_digest: str
    base_oid: str
    integration_oid: str | None
    artifact_digest: str | None
    candidate_set_digest: str | None
    origin: str
    payload_json: str
    payload_digest: str
    raw_output_sha256: str
    raw_output_kind: str
    raw_output_framed_sha256: str
    operation_fingerprint: str
    contract_digest_input: str | None
    created_at_policy: str
    compatibility_error: str | None
    compatibility_dispatch_state: str | None
    compatibility_delegation_ids_json: str | None
    compatibility_sandbox_workspace: str | None
    compatibility_clear_dispatch_owner: int
    created_at_ns: int

    @classmethod
    def from_row(cls, row: sqlite3.Row | Mapping[str, Any]) -> "ProofEventReceipt":
        values = dict(row)
        return cls(**{name: values[name] for name in cls.__dataclass_fields__})

    def to_dict(self) -> dict[str, Any]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        }


_LOCAL_TERMINAL_AUTHORITY_FIELDS = (
    "integration_oid",
    "artifact_digest",
    "candidate_set_digest",
    "proof_authority_epoch",
    "proof_event_seq",
    "proof_event_hash",
    "verification_receipt_json",
    "verification_receipt_digest",
    "tests_verified_at",
    "review_verified_at",
    "remote_verified_at",
    "live_verified_at",
    "completed_at",
    "verified_at",
)
_LOCAL_POST_LANDING_PUSH_STATES = frozenset(
    {"awaiting", "pushing", "effect_unknown", "pushed", "declined", "expired", "stale"}
)
_LOCAL_FAILED_PUSH_STATES = frozenset({"expired", "not_landed", "stale"})


def _validate_local_terminal_overlay(
    values: Mapping[str, Any],
    validated_plan: Any,
    authority: list[ProofEventReceipt],
    advisory: list[ProofEventReceipt],
) -> bool:
    """Accept only the exact host-owned terminal shape for local ``go``."""

    contract = getattr(validated_plan, "contract", None)
    state = values.get("state")
    is_local = (
        isinstance(contract, Mapping)
        and contract.get("schema") == "hermes.bestplan.local-go.v1"
        and contract.get("version") == 1
        and contract.get("mode") == "local_main"
    )
    if not is_local or state not in {"completed_local", "failed"}:
        return False
    if authority:
        raise ProofValidationError("local terminal overlay has authority events")
    if (
        values.get("current_phase") != "captured"
        or any(values.get(name) is not None for name in _LOCAL_TERMINAL_AUTHORITY_FIELDS)
    ):
        raise ProofValidationError("local terminal overlay has authority projection")
    if values.get("dispatch_state") != "terminal" or values.get("dispatch_owner") is not None:
        raise ProofValidationError("local terminal overlay dispatch is not terminal")
    if not advisory:
        raise ProofValidationError("local terminal overlay has no advisory")

    latest = advisory[-1]
    push_state = values.get("local_push_state")
    push_json = values.get("local_push_json")
    if state == "completed_local":
        if push_state not in _LOCAL_POST_LANDING_PUSH_STATES or not isinstance(
            push_json, str,
        ):
            raise ProofValidationError("local landing push projection is invalid")
        expected_kind = "local_landing_recovered_advisory"
        expected_error = None
        expected_source = "process"
        expected_output = {"status": "local_landing_recovered"}
    elif push_json is None and push_state is None:
        expected_kind = "local_execution_failed_advisory"
        expected_error = "dispatch_failed"
        expected_source = "async"
        expected_output = {"status": "local_execution_failed"}
    else:
        if push_state not in _LOCAL_FAILED_PUSH_STATES or not isinstance(push_json, str):
            raise ProofValidationError("local failure push projection is invalid")
        expected_kind = "local_execution_reconciled_failed_advisory"
        expected_error = "dispatch_failed"
        expected_source = "process"
        expected_output = {
            "status": "local_execution_failed",
            "local_push_state": push_state,
        }

    if push_json is not None:
        try:
            from agent.bestplan_local_push import decode_local_push_row

            decode_local_push_row(values, lambda _row: validated_plan)
        except Exception:
            raise ProofValidationError(
                "local terminal push record failed revalidation"
            ) from None
    if (
        latest.kind != expected_kind
        or latest.previous_phase != "captured"
        or latest.phase != "captured"
        or latest.projected_state not in {"running", "waiting"}
        or latest.origin != "gateway"
        or latest.approval_digest != values.get("approval_digest")
        or latest.contract_digest != values.get("promotion_contract_digest")
        or latest.source_snapshot_digest != values.get("source_snapshot_digest")
        or latest.base_oid != values.get("baseline_revision")
        or latest.integration_oid is not None
        or latest.artifact_digest is not None
        or latest.candidate_set_digest is not None
        or latest.contract_digest_input is not None
        or latest.compatibility_error != expected_error
        or latest.compatibility_dispatch_state != "terminal"
        or latest.compatibility_delegation_ids_json is not None
        or latest.compatibility_sandbox_workspace is not None
        or latest.compatibility_clear_dispatch_owner != 1
    ):
        raise ProofValidationError("local terminal advisory differs")
    try:
        expected_redaction = redact_output(expected_output, source=expected_source)
    except RedactionError:
        raise ProofValidationError("local terminal advisory cannot be redacted") from None
    if (
        latest.payload_json != expected_redaction.canonical_json
        or latest.payload_digest
        != _digest(expected_redaction.canonical_json, REDACTED_DIGEST_DOMAIN)
        or latest.raw_output_sha256 != expected_redaction.raw_sha256
        or latest.raw_output_kind != expected_redaction.raw_kind
        or latest.raw_output_framed_sha256 != expected_redaction.raw_framed_sha256
    ):
        raise ProofValidationError("local terminal advisory payload differs")
    return True


@dataclass(frozen=True)
class CandidateReceipt:
    plan_id: str
    candidate_id: str
    slice_id: str
    attempt_id: str
    commit_oid: str
    tree_oid: str
    base_oid: str
    approval_digest: str
    contract_digest: str
    source_snapshot_digest: str
    receipt_json: str
    receipt_digest: str
    raw_output_sha256: str
    raw_output_kind: str
    raw_output_framed_sha256: str
    created_at_policy: str
    created_at_ns: int

    @classmethod
    def from_row(cls, row: sqlite3.Row | Mapping[str, Any]) -> "CandidateReceipt":
        values = dict(row)
        return cls(**{name: values[name] for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class AuthorityVerification:
    receipt_json: str
    receipt_digest: str
    plan_id: str
    authority_epoch: str
    event_seq: int
    event_hash: str
    approval_digest: str
    contract_digest: str
    source_snapshot_digest: str
    integration_oid: str
    artifact_digest: str
    candidate_set_digest: str
    issued_at_ns: int
    expires_at_ns: int

    def __post_init__(self) -> None:
        if not isinstance(self.receipt_json, str) or len(self.receipt_json) > 32768:
            raise ProofValidationError("authority receipt JSON is not bounded")
        try:
            body = json.loads(self.receipt_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProofValidationError("authority receipt JSON is invalid") from exc
        if _canonical_json(body) != self.receipt_json:
            raise ProofValidationError("authority receipt JSON is not canonical")
        if type(body) is not dict or frozenset(body) != _AUTHORITY_RECEIPT_KEYS:
            raise ProofValidationError("authority receipt shape differs")
        if _digest(self.receipt_json, AUTHORITY_RECEIPT_DOMAIN) != self.receipt_digest:
            raise ProofValidationError("authority receipt digest differs")
        expected = {
            "plan_id": self.plan_id,
            "authority_epoch": self.authority_epoch,
            "event_seq": self.event_seq,
            "event_hash": self.event_hash,
            "approval_digest": self.approval_digest,
            "promotion_contract_digest": self.contract_digest,
            "source_snapshot_digest": self.source_snapshot_digest,
            "integration_oid": self.integration_oid,
            "artifact_digest": self.artifact_digest,
            "candidate_set_digest": self.candidate_set_digest,
            "issued_at_ns": self.issued_at_ns,
            "expires_at_ns": self.expires_at_ns,
        }
        for key, value in expected.items():
            if body.get(key) != value:
                raise ProofValidationError("authority receipt binding differs")
        if body.get("schema") != AUTHORITY_RECEIPT_SCHEMA:
            raise ProofValidationError("authority receipt schema is unsupported")
        if body.get("version") != 1 or body.get("kind") != "live_verified":
            raise ProofValidationError("authority receipt kind/version is unsupported")

    def to_dict(self) -> dict[str, Any]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        }


_EVENT_COLUMNS: dict[str, str] = {
    "plan_id": "TEXT",
    "stream": "TEXT",
    "authority_epoch": "TEXT",
    "event_seq": "INTEGER",
    "event_hash": "TEXT",
    "previous_hash": "TEXT",
    "operation_id": "TEXT",
    "protocol": "INTEGER",
    "kind": "TEXT",
    "previous_phase": "TEXT",
    "phase": "TEXT",
    "projected_state": "TEXT",
    "approval_digest": "TEXT",
    "contract_digest": "TEXT",
    "source_snapshot_digest": "TEXT",
    "base_oid": "TEXT",
    "integration_oid": "TEXT",
    "artifact_digest": "TEXT",
    "candidate_set_digest": "TEXT",
    "compatibility_dispatch_state": "TEXT",
    "compatibility_delegation_ids_json": "TEXT",
    "compatibility_sandbox_workspace": "TEXT",
    "compatibility_clear_dispatch_owner": "INTEGER",
    "origin": "TEXT",
    "payload_json": "TEXT",
    "payload_digest": "TEXT",
    "raw_output_sha256": "TEXT",
    "raw_output_kind": "TEXT",
    "raw_output_framed_sha256": "TEXT",
    "operation_fingerprint": "TEXT",
    "contract_digest_input": "TEXT",
    "created_at_policy": "TEXT",
    "compatibility_error": "TEXT",
    "created_at_ns": "INTEGER",
}
_CANDIDATE_COLUMNS: dict[str, str] = {
    "plan_id": "TEXT",
    "candidate_id": "TEXT",
    "slice_id": "TEXT",
    "attempt_id": "TEXT",
    "commit_oid": "TEXT",
    "tree_oid": "TEXT",
    "base_oid": "TEXT",
    "approval_digest": "TEXT",
    "contract_digest": "TEXT",
    "source_snapshot_digest": "TEXT",
    "receipt_json": "TEXT",
    "receipt_digest": "TEXT",
    "raw_output_sha256": "TEXT",
    "raw_output_kind": "TEXT",
    "raw_output_framed_sha256": "TEXT",
    "created_at_policy": "TEXT",
    "created_at_ns": "INTEGER",
}
_VERIFICATION_COLUMNS: dict[str, str] = {
    "plan_id": "TEXT",
    "authority_epoch": "TEXT",
    "event_seq": "INTEGER",
    "event_hash": "TEXT",
    "approval_digest": "TEXT",
    "contract_digest": "TEXT",
    "source_snapshot_digest": "TEXT",
    "base_oid": "TEXT",
    "integration_oid": "TEXT",
    "artifact_digest": "TEXT",
    "candidate_set_digest": "TEXT",
    "receipt_json": "TEXT",
    "receipt_digest": "TEXT",
    "verified_at_ns": "INTEGER",
}

_CREATE_EVENTS_SQL = """
CREATE TABLE bestplan_proof_events (
    plan_id TEXT NOT NULL,
    stream TEXT NOT NULL CHECK (stream IN ('authority', 'advisory')),
    authority_epoch TEXT NOT NULL,
    event_seq INTEGER NOT NULL CHECK (event_seq >= 1),
    event_hash TEXT NOT NULL,
    previous_hash TEXT,
    operation_id TEXT NOT NULL,
    protocol INTEGER NOT NULL CHECK (protocol = 2),
    kind TEXT NOT NULL,
    previous_phase TEXT NOT NULL,
    phase TEXT NOT NULL,
    projected_state TEXT NOT NULL,
    approval_digest TEXT NOT NULL,
    contract_digest TEXT NOT NULL,
    source_snapshot_digest TEXT NOT NULL,
    base_oid TEXT NOT NULL,
    integration_oid TEXT,
    artifact_digest TEXT,
    candidate_set_digest TEXT,
    origin TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    raw_output_sha256 TEXT NOT NULL,
    raw_output_kind TEXT NOT NULL,
    raw_output_framed_sha256 TEXT NOT NULL,
    operation_fingerprint TEXT NOT NULL,
    contract_digest_input TEXT,
    created_at_policy TEXT NOT NULL CHECK (created_at_policy IN ('clock', 'explicit')),
    compatibility_error TEXT,
    compatibility_dispatch_state TEXT,
    compatibility_delegation_ids_json TEXT,
    compatibility_sandbox_workspace TEXT,
    compatibility_clear_dispatch_owner INTEGER NOT NULL DEFAULT 0
        CHECK (compatibility_clear_dispatch_owner IN (0, 1)),
    created_at_ns INTEGER NOT NULL CHECK (created_at_ns >= 0)
)
"""
_CREATE_CANDIDATES_SQL = """
CREATE TABLE bestplan_candidates (
    plan_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    slice_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    commit_oid TEXT NOT NULL,
    tree_oid TEXT NOT NULL,
    base_oid TEXT NOT NULL,
    approval_digest TEXT NOT NULL,
    contract_digest TEXT NOT NULL,
    source_snapshot_digest TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    receipt_digest TEXT NOT NULL,
    raw_output_sha256 TEXT NOT NULL,
    raw_output_kind TEXT NOT NULL,
    raw_output_framed_sha256 TEXT NOT NULL,
    created_at_policy TEXT NOT NULL CHECK (created_at_policy IN ('clock', 'explicit')),
    created_at_ns INTEGER NOT NULL CHECK (created_at_ns >= 0)
)
"""
_CREATE_VERIFICATIONS_SQL = """
CREATE TABLE bestplan_verification_receipts (
    plan_id TEXT NOT NULL,
    authority_epoch TEXT NOT NULL,
    event_seq INTEGER NOT NULL CHECK (event_seq >= 1),
    event_hash TEXT NOT NULL,
    approval_digest TEXT NOT NULL,
    contract_digest TEXT NOT NULL,
    source_snapshot_digest TEXT NOT NULL,
    base_oid TEXT NOT NULL,
    integration_oid TEXT NOT NULL,
    artifact_digest TEXT NOT NULL,
    candidate_set_digest TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    receipt_digest TEXT NOT NULL,
    verified_at_ns INTEGER NOT NULL CHECK (verified_at_ns >= 0)
)
"""

_INDEX_SQL = {
    "bestplan_proof_events_head_v1": (
        "CREATE UNIQUE INDEX bestplan_proof_events_head_v1 ON "
        "bestplan_proof_events(plan_id, stream, authority_epoch, event_seq)"
    ),
    "bestplan_proof_events_operation_v1": (
        "CREATE UNIQUE INDEX bestplan_proof_events_operation_v1 ON "
        "bestplan_proof_events(plan_id, stream, authority_epoch, operation_id)"
    ),
    "bestplan_candidates_identity_v1": (
        "CREATE UNIQUE INDEX bestplan_candidates_identity_v1 ON "
        "bestplan_candidates(plan_id, candidate_id)"
    ),
    "bestplan_candidates_attempt_v1": (
        "CREATE UNIQUE INDEX bestplan_candidates_attempt_v1 ON "
        "bestplan_candidates(plan_id, attempt_id)"
    ),
    "bestplan_verification_receipts_plan_v1": (
        "CREATE UNIQUE INDEX bestplan_verification_receipts_plan_v1 ON "
        "bestplan_verification_receipts(plan_id)"
    ),
}


def _edge_sql(prefix: str = "NEW") -> str:
    return " OR ".join(
        f"({prefix}.previous_phase='{left}' AND {prefix}.phase='{right}')"
        for left, right in sorted(AUTHORITY_PHASE_EDGES)
    )


_TRIGGER_SQL = {
    "bestplan_proof_events_shape_guard_v1": """
CREATE TRIGGER bestplan_proof_events_shape_guard_v1
BEFORE INSERT ON bestplan_proof_events
BEGIN
    SELECT CASE WHEN (
        length(NEW.plan_id) BETWEEN 1 AND 512
        AND instr(NEW.plan_id, char(0)) = 0
        AND NEW.stream IN ('authority','advisory')
        AND length(NEW.authority_epoch) BETWEEN 1 AND 512
        AND instr(NEW.authority_epoch, char(0)) = 0
        AND typeof(NEW.event_seq) = 'integer'
        AND NEW.event_seq >= 1
        AND length(NEW.event_hash) = 64
        AND NEW.event_hash NOT GLOB '*[^0-9a-f]*'
        AND (NEW.previous_hash IS NULL OR (
            length(NEW.previous_hash) = 64
            AND NEW.previous_hash NOT GLOB '*[^0-9a-f]*'))
        AND length(NEW.operation_id) = 36
        AND substr(NEW.operation_id,9,1) = '-'
        AND substr(NEW.operation_id,14,1) = '-'
        AND substr(NEW.operation_id,19,1) = '-'
        AND substr(NEW.operation_id,24,1) = '-'
        AND length(replace(NEW.operation_id,'-','')) = 32
        AND replace(NEW.operation_id,'-','') NOT GLOB '*[^0-9a-f]*'
        AND typeof(NEW.protocol) = 'integer'
        AND NEW.protocol = 2
        AND length(NEW.kind) BETWEEN 1 AND 64
        AND substr(NEW.kind,1,1) GLOB '[a-z]'
        AND NEW.kind NOT GLOB '*[^a-z0-9_-]*'
        AND length(NEW.previous_phase) BETWEEN 1 AND 64
        AND substr(NEW.previous_phase,1,1) GLOB '[a-z]'
        AND NEW.previous_phase NOT GLOB '*[^a-z0-9_-]*'
        AND length(NEW.phase) BETWEEN 1 AND 64
        AND substr(NEW.phase,1,1) GLOB '[a-z]'
        AND NEW.phase NOT GLOB '*[^a-z0-9_-]*'
        AND length(NEW.projected_state) BETWEEN 1 AND 64
        AND substr(NEW.projected_state,1,1) GLOB '[a-z]'
        AND NEW.projected_state NOT GLOB '*[^a-z0-9_-]*'
        AND length(NEW.approval_digest) = 64
        AND NEW.approval_digest NOT GLOB '*[^0-9a-f]*'
        AND length(NEW.contract_digest) = 64
        AND NEW.contract_digest NOT GLOB '*[^0-9a-f]*'
        AND length(NEW.source_snapshot_digest) = 64
        AND NEW.source_snapshot_digest NOT GLOB '*[^0-9a-f]*'
        AND length(NEW.base_oid) IN (40,64)
        AND NEW.base_oid NOT GLOB '*[^0-9a-f]*'
        AND (NEW.integration_oid IS NULL OR (
            length(NEW.integration_oid) IN (40,64)
            AND NEW.integration_oid NOT GLOB '*[^0-9a-f]*'))
        AND (NEW.artifact_digest IS NULL OR (
            length(NEW.artifact_digest) = 64
            AND NEW.artifact_digest NOT GLOB '*[^0-9a-f]*'))
        AND length(NEW.payload_json) BETWEEN 1 AND 32768
        AND length(NEW.payload_digest) = 64
        AND NEW.payload_digest NOT GLOB '*[^0-9a-f]*'
        AND length(NEW.raw_output_sha256) = 64
        AND NEW.raw_output_sha256 NOT GLOB '*[^0-9a-f]*'
        AND NEW.raw_output_kind IN ('null','boolean','integer','string','bytes','list','mapping')
        AND length(NEW.raw_output_framed_sha256) = 64
        AND NEW.raw_output_framed_sha256 NOT GLOB '*[^0-9a-f]*'
        AND length(NEW.operation_fingerprint) = 64
        AND NEW.operation_fingerprint NOT GLOB '*[^0-9a-f]*'
        AND (NEW.candidate_set_digest IS NULL OR (
            length(NEW.candidate_set_digest) = 64
            AND NEW.candidate_set_digest NOT GLOB '*[^0-9a-f]*'))
        AND length(NEW.origin) BETWEEN 1 AND 64
        AND substr(NEW.origin,1,1) GLOB '[a-z]'
        AND NEW.origin NOT GLOB '*[^a-z0-9_-]*'
        AND (NEW.contract_digest_input IS NULL OR (
            length(NEW.contract_digest_input) = 64
            AND NEW.contract_digest_input NOT GLOB '*[^0-9a-f]*'))
        AND (NEW.compatibility_error IS NULL OR
             NEW.compatibility_error IN
                 ('dispatch_deferred','dispatch_failed','dispatch_unknown',
                  'recapture_required','recovered_dead_dispatch_owner',
                  'recovered_pre_run_schedule'))
        AND (NEW.compatibility_dispatch_state IS NULL OR
             NEW.compatibility_dispatch_state IN
                 ('intent','dispatching','scheduled','unknown','terminal'))
        AND (NEW.compatibility_delegation_ids_json IS NULL OR
             (length(NEW.compatibility_delegation_ids_json) BETWEEN 2 AND 1024
              AND NEW.compatibility_delegation_ids_json = (
                  SELECT '["' || p.dispatch_id || '"]'
                  FROM bestplan_plans AS p
                  WHERE p.plan_id = NEW.plan_id
                    AND p.execution_protocol = 2
                    AND p.dispatch_id = 'bestplan-' || p.plan_id
              )))
        AND (NEW.compatibility_sandbox_workspace IS NULL OR
             NEW.compatibility_sandbox_workspace = '')
        AND typeof(NEW.compatibility_clear_dispatch_owner) = 'integer'
        AND NEW.compatibility_clear_dispatch_owner IN (0,1)
        AND NEW.created_at_policy IN ('clock','explicit')
        AND typeof(NEW.created_at_ns) = 'integer'
        AND NEW.created_at_ns >= 0
    ) IS NOT TRUE THEN RAISE(ABORT, 'invalid bestplan proof event shape') END;
END
""",
    "bestplan_candidates_shape_guard_v1": """
CREATE TRIGGER bestplan_candidates_shape_guard_v1
BEFORE INSERT ON bestplan_candidates
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM bestplan_candidates AS c
        WHERE c.plan_id = NEW.plan_id
          AND (c.candidate_id = NEW.candidate_id OR c.attempt_id = NEW.attempt_id)
    ) THEN RAISE(ABORT, 'bestplan candidate identity already exists') END;
    SELECT CASE WHEN (
        length(NEW.plan_id) BETWEEN 1 AND 512
        AND instr(NEW.plan_id, char(0)) = 0
        AND length(NEW.candidate_id) BETWEEN 1 AND 512
        AND instr(NEW.candidate_id, char(0)) = 0
        AND length(NEW.slice_id) BETWEEN 1 AND 512
        AND instr(NEW.slice_id, char(0)) = 0
        AND length(NEW.attempt_id) BETWEEN 1 AND 512
        AND instr(NEW.attempt_id, char(0)) = 0
        AND length(NEW.commit_oid) IN (40,64)
        AND NEW.commit_oid NOT GLOB '*[^0-9a-f]*'
        AND length(NEW.tree_oid) IN (40,64)
        AND NEW.tree_oid NOT GLOB '*[^0-9a-f]*'
        AND length(NEW.base_oid) IN (40,64)
        AND NEW.base_oid NOT GLOB '*[^0-9a-f]*'
        AND length(NEW.approval_digest) = 64
        AND NEW.approval_digest NOT GLOB '*[^0-9a-f]*'
        AND length(NEW.contract_digest) = 64
        AND NEW.contract_digest NOT GLOB '*[^0-9a-f]*'
        AND length(NEW.source_snapshot_digest) = 64
        AND NEW.source_snapshot_digest NOT GLOB '*[^0-9a-f]*'
        AND length(NEW.receipt_json) BETWEEN 1 AND 32768
        AND length(NEW.receipt_digest) = 64
        AND NEW.receipt_digest NOT GLOB '*[^0-9a-f]*'
        AND length(NEW.raw_output_sha256) = 64
        AND NEW.raw_output_sha256 NOT GLOB '*[^0-9a-f]*'
        AND NEW.raw_output_kind IN ('null','boolean','integer','string','bytes','list','mapping')
        AND length(NEW.raw_output_framed_sha256) = 64
        AND NEW.raw_output_framed_sha256 NOT GLOB '*[^0-9a-f]*'
        AND NEW.created_at_policy IN ('clock','explicit')
        AND typeof(NEW.created_at_ns) = 'integer'
        AND NEW.created_at_ns >= 0
    ) IS NOT TRUE THEN RAISE(ABORT, 'invalid bestplan candidate shape') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM bestplan_plans AS p
        WHERE p.plan_id = NEW.plan_id
          AND p.execution_protocol = 2
          AND p.baseline_revision IS NEW.base_oid
          AND p.approval_digest IS NEW.approval_digest
          AND p.promotion_contract_digest IS NEW.contract_digest
          AND p.source_snapshot_digest IS NEW.source_snapshot_digest
          AND p.current_phase = 'captured'
          AND p.proof_event_seq IS NULL
          AND p.proof_event_hash IS NULL
          AND p.proof_authority_epoch IS NULL
          AND p.candidate_set_digest IS NULL
    ) THEN RAISE(ABORT, 'bestplan candidate does not bind captured source') END;
END
""",
    "bestplan_proof_events_no_update_v1": """
CREATE TRIGGER bestplan_proof_events_no_update_v1
BEFORE UPDATE ON bestplan_proof_events
BEGIN
    SELECT RAISE(ABORT, 'bestplan proof events are append-only');
END
""",
    "bestplan_proof_events_no_delete_v1": """
CREATE TRIGGER bestplan_proof_events_no_delete_v1
BEFORE DELETE ON bestplan_proof_events
BEGIN
    SELECT RAISE(ABORT, 'bestplan proof events are append-only');
END
""",
    "bestplan_candidates_no_update_v1": """
CREATE TRIGGER bestplan_candidates_no_update_v1
BEFORE UPDATE ON bestplan_candidates
BEGIN
    SELECT RAISE(ABORT, 'bestplan candidates are append-only');
END
""",
    "bestplan_candidates_no_delete_v1": """
CREATE TRIGGER bestplan_candidates_no_delete_v1
BEFORE DELETE ON bestplan_candidates
BEGIN
    SELECT RAISE(ABORT, 'bestplan candidates are append-only');
END
""",
    "bestplan_verification_receipts_shape_guard_v1": """
CREATE TRIGGER bestplan_verification_receipts_shape_guard_v1
BEFORE INSERT ON bestplan_verification_receipts
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM bestplan_verification_receipts AS r
        WHERE r.plan_id = NEW.plan_id
    ) THEN RAISE(ABORT, 'bestplan verification receipt already exists') END;
    SELECT CASE WHEN (
        length(NEW.event_hash) = 64
        AND NEW.event_hash NOT GLOB '*[^0-9a-f]*'
        AND length(NEW.approval_digest) = 64
        AND NEW.approval_digest NOT GLOB '*[^0-9a-f]*'
        AND length(NEW.contract_digest) = 64
        AND NEW.contract_digest NOT GLOB '*[^0-9a-f]*'
        AND length(NEW.source_snapshot_digest) = 64
        AND NEW.source_snapshot_digest NOT GLOB '*[^0-9a-f]*'
        AND length(NEW.base_oid) IN (40, 64)
        AND NEW.base_oid NOT GLOB '*[^0-9a-f]*'
        AND length(NEW.integration_oid) IN (40, 64)
        AND NEW.integration_oid NOT GLOB '*[^0-9a-f]*'
        AND length(NEW.artifact_digest) = 64
        AND NEW.artifact_digest NOT GLOB '*[^0-9a-f]*'
        AND length(NEW.candidate_set_digest) = 64
        AND NEW.candidate_set_digest NOT GLOB '*[^0-9a-f]*'
        AND length(NEW.receipt_json) BETWEEN 1 AND 32768
        AND length(NEW.receipt_digest) = 64
        AND NEW.receipt_digest NOT GLOB '*[^0-9a-f]*'
        AND NEW.verified_at_ns >= 0
    ) IS NOT TRUE THEN RAISE(ABORT, 'invalid bestplan verification receipt shape') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM bestplan_plans AS p
        JOIN bestplan_proof_events AS e
          ON e.plan_id = p.plan_id
         AND e.stream = 'authority'
         AND e.origin = 'authority'
         AND e.kind = 'live_verified'
         AND e.phase = 'live_verified'
         AND e.authority_epoch IS NEW.authority_epoch
         AND e.event_seq IS NEW.event_seq
         AND e.event_hash IS NEW.event_hash
         AND e.approval_digest IS NEW.approval_digest
         AND e.contract_digest IS NEW.contract_digest
         AND e.source_snapshot_digest IS NEW.source_snapshot_digest
         AND e.base_oid IS NEW.base_oid
         AND e.integration_oid IS NEW.integration_oid
         AND e.artifact_digest IS NEW.artifact_digest
         AND e.candidate_set_digest IS NEW.candidate_set_digest
         AND e.projected_state = 'completed_unverified'
        WHERE p.plan_id = NEW.plan_id
          AND p.execution_protocol = 2
          AND p.state = 'completed_unverified'
          AND p.promotion_mode = 'auto_live'
          AND p.current_phase = 'live_verified'
          AND p.live_verified_at IS NOT NULL
          AND p.verified_at IS NULL
          AND p.verification_receipt_json IS NULL
          AND p.verification_receipt_digest IS NULL
          AND p.approval_digest IS NEW.approval_digest
          AND p.promotion_contract_digest IS NEW.contract_digest
          AND p.source_snapshot_digest IS NEW.source_snapshot_digest
          AND p.baseline_revision IS NEW.base_oid
          AND p.integration_oid IS NEW.integration_oid
          AND p.artifact_digest IS NEW.artifact_digest
          AND p.candidate_set_digest IS NEW.candidate_set_digest
          AND p.proof_authority_epoch IS NEW.authority_epoch
          AND p.proof_event_seq IS NEW.event_seq
          AND p.proof_event_hash IS NEW.event_hash
          AND EXISTS (
              SELECT 1 FROM bestplan_candidates AS c
              WHERE c.plan_id = p.plan_id
                AND c.base_oid IS NEW.base_oid
                AND c.approval_digest IS NEW.approval_digest
                AND c.contract_digest IS NEW.contract_digest
                AND c.source_snapshot_digest IS NEW.source_snapshot_digest
          )
    ) THEN RAISE(ABORT, 'bestplan verification receipt does not bind terminal proof') END;
END
""",
    "bestplan_verification_receipts_no_update_v1": """
CREATE TRIGGER bestplan_verification_receipts_no_update_v1
BEFORE UPDATE ON bestplan_verification_receipts
BEGIN
    SELECT RAISE(ABORT, 'bestplan verification receipts are append-only');
END
""",
    "bestplan_verification_receipts_no_delete_v1": """
CREATE TRIGGER bestplan_verification_receipts_no_delete_v1
BEFORE DELETE ON bestplan_verification_receipts
BEGIN
    SELECT RAISE(ABORT, 'bestplan verification receipts are append-only');
END
""",
    "bestplan_proof_authority_insert_guard_v1": f"""
CREATE TRIGGER bestplan_proof_authority_insert_guard_v1
BEFORE INSERT ON bestplan_proof_events
WHEN NEW.stream = 'authority'
BEGIN
    SELECT CASE WHEN NOT ({_edge_sql()})
        THEN RAISE(ABORT, 'invalid bestplan authority phase edge') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM bestplan_plans AS p
        WHERE p.plan_id = NEW.plan_id
          AND p.execution_protocol = 2
          AND NEW.protocol = 2
          AND p.approval_digest IS NEW.approval_digest
          AND p.promotion_contract_digest IS NEW.contract_digest
          AND p.source_snapshot_digest IS NEW.source_snapshot_digest
          AND p.baseline_revision IS NEW.base_oid
          AND p.candidate_set_digest IS NEW.candidate_set_digest
          AND NEW.candidate_set_digest IS NOT NULL
          AND p.current_phase IS NEW.previous_phase
          AND p.state = 'running'
          AND p.state IS NOT 'completed_verified'
          AND NEW.kind IS NEW.phase
          AND NEW.origin IN ('promoter','authority')
          AND (NEW.phase IS NOT 'live_verified' OR NEW.origin = 'authority')
          AND (
              (NEW.phase = 'live_verified'
               AND NEW.projected_state = 'completed_unverified')
              OR
              (NEW.phase IS NOT 'live_verified'
               AND NEW.projected_state = 'running')
          )
          AND (
              (NEW.phase IN ('candidate_ready','queued','integrating')
               AND NEW.integration_oid IS NULL
               AND NEW.artifact_digest IS NULL)
              OR
              (NEW.phase IN ('integrated_proven','testing','tests_verified',
                             'reviewing','review_verified')
               AND NEW.integration_oid IS NOT NULL
               AND NEW.artifact_digest IS NULL)
              OR
              (NEW.phase IN ('artifact_frozen','main_fast_forwarded',
                             'remote_verified','deploying','live_verified')
               AND NEW.integration_oid IS NOT NULL
               AND NEW.artifact_digest IS NOT NULL)
          )
          AND (p.integration_oid IS NULL OR p.integration_oid IS NEW.integration_oid)
          AND (p.artifact_digest IS NULL OR p.artifact_digest IS NEW.artifact_digest)
          AND (
              p.proof_event_seq IS NULL
              OR NEW.created_at_ns >= (
                  SELECT h.created_at_ns FROM bestplan_proof_events AS h
                  WHERE h.plan_id = p.plan_id
                    AND h.stream = 'authority'
                    AND h.authority_epoch IS p.proof_authority_epoch
                    AND h.event_seq IS p.proof_event_seq
                    AND h.event_hash IS p.proof_event_hash
              )
          )
          AND (
              (p.proof_event_seq IS NULL
               AND p.proof_event_hash IS NULL
               AND p.proof_authority_epoch IS NULL
               AND NEW.event_seq = 1
               AND NEW.previous_hash IS NULL)
              OR
              (p.proof_authority_epoch IS NEW.authority_epoch
               AND NEW.event_seq = p.proof_event_seq + 1
               AND NEW.previous_hash IS p.proof_event_hash)
          )
    ) THEN RAISE(ABORT, 'bestplan authority event does not bind current projection') END;
END
""",
    "bestplan_proof_advisory_insert_guard_v1": """
CREATE TRIGGER bestplan_proof_advisory_insert_guard_v1
BEFORE INSERT ON bestplan_proof_events
WHEN NEW.stream = 'advisory'
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM bestplan_plans AS p
        WHERE p.plan_id = NEW.plan_id
          AND p.execution_protocol = 2
          AND NEW.protocol = 2
          AND COALESCE(p.approval_digest, '0000000000000000000000000000000000000000000000000000000000000000') IS NEW.approval_digest
          AND COALESCE(p.promotion_contract_digest, '0000000000000000000000000000000000000000000000000000000000000000') IS NEW.contract_digest
          AND COALESCE(p.source_snapshot_digest, '0000000000000000000000000000000000000000000000000000000000000000') IS NEW.source_snapshot_digest
          AND COALESCE(p.baseline_revision, '0000000000000000000000000000000000000000') IS NEW.base_oid
          AND p.current_phase IS NEW.previous_phase
          AND p.current_phase IS NEW.phase
          AND p.state IS NEW.projected_state
          AND p.integration_oid IS NEW.integration_oid
          AND p.artifact_digest IS NEW.artifact_digest
          AND p.candidate_set_digest IS NEW.candidate_set_digest
          AND NEW.event_seq = COALESCE((
              SELECT MAX(e.event_seq) FROM bestplan_proof_events AS e
              WHERE e.plan_id = NEW.plan_id AND e.stream = 'advisory'
                AND e.authority_epoch = NEW.authority_epoch
          ), 0) + 1
          AND NEW.previous_hash IS (
              SELECT e.event_hash FROM bestplan_proof_events AS e
              WHERE e.plan_id = NEW.plan_id AND e.stream = 'advisory'
                AND e.authority_epoch = NEW.authority_epoch
              ORDER BY e.event_seq DESC LIMIT 1
          )
    ) THEN RAISE(ABORT, 'bestplan advisory event does not bind current projection') END;
END
""",
    "bestplan_plans_v2_candidate_set_guard_v1": """
CREATE TRIGGER bestplan_plans_v2_candidate_set_guard_v1
BEFORE UPDATE ON bestplan_plans
WHEN OLD.execution_protocol = 2
 AND NEW.candidate_set_digest IS NOT OLD.candidate_set_digest
BEGIN
    SELECT CASE WHEN NOT (
        OLD.current_phase = 'captured'
        AND OLD.proof_event_seq IS NULL
        AND OLD.proof_event_hash IS NULL
        AND OLD.proof_authority_epoch IS NULL
        AND OLD.candidate_set_digest IS NULL
        AND length(NEW.candidate_set_digest) = 64
        AND NEW.candidate_set_digest NOT GLOB '*[^0-9a-f]*'
        AND EXISTS (
            SELECT 1 FROM bestplan_candidates AS c WHERE c.plan_id = OLD.plan_id
        )
    ) THEN RAISE(ABORT, 'bestplan candidate set can only freeze at capture') END;
END
""",
    "bestplan_plans_v2_immutable_inputs_v1": """
CREATE TRIGGER bestplan_plans_v2_immutable_inputs_v1
BEFORE UPDATE ON bestplan_plans
WHEN OLD.execution_protocol = 2 AND (
       NEW.version IS NOT OLD.version
    OR NEW.execution_protocol IS NOT OLD.execution_protocol
    OR NEW.plan_id IS NOT OLD.plan_id
    OR NEW.session_id IS NOT OLD.session_id
    OR NEW.profile IS NOT OLD.profile
    OR NEW.workspace IS NOT OLD.workspace
    OR NEW.baseline_revision IS NOT OLD.baseline_revision
    OR NEW.baseline_fingerprint IS NOT OLD.baseline_fingerprint
    OR NEW.raw_plan_json IS NOT OLD.raw_plan_json
    OR NEW.validated_manifest_json IS NOT OLD.validated_manifest_json
    OR NEW.approval_digest IS NOT OLD.approval_digest
    OR NEW.promotion_contract_version IS NOT OLD.promotion_contract_version
    OR NEW.promotion_contract_json IS NOT OLD.promotion_contract_json
    OR NEW.promotion_contract_digest IS NOT OLD.promotion_contract_digest
    OR NEW.source_snapshot_json IS NOT OLD.source_snapshot_json
    OR NEW.source_snapshot_digest IS NOT OLD.source_snapshot_digest
    OR NEW.promotion_mode IS NOT OLD.promotion_mode
)
BEGIN
    SELECT RAISE(ABORT, 'bestplan protocol-2 approval inputs are immutable');
END
""",
    "bestplan_plans_v2_terminal_guard_v1": """
CREATE TRIGGER bestplan_plans_v2_terminal_guard_v1
BEFORE UPDATE ON bestplan_plans
WHEN OLD.execution_protocol = 2
 AND NEW.state = 'completed_verified'
 AND OLD.state IS NOT 'completed_verified'
BEGIN
    SELECT CASE WHEN NOT (
        OLD.state = 'completed_unverified'
        AND OLD.promotion_mode = 'auto_live'
        AND OLD.current_phase = 'live_verified'
        AND OLD.live_verified_at IS NOT NULL
        AND OLD.verified_at IS NULL
        AND NEW.verified_at IS NOT NULL
        AND NEW.verification_receipt_json IS NOT NULL
        AND NEW.verification_receipt_digest IS NOT NULL
        AND NEW.current_phase IS OLD.current_phase
        AND NEW.integration_oid IS OLD.integration_oid
        AND NEW.artifact_digest IS OLD.artifact_digest
        AND NEW.proof_authority_epoch IS OLD.proof_authority_epoch
        AND NEW.proof_event_seq IS OLD.proof_event_seq
        AND NEW.proof_event_hash IS OLD.proof_event_hash
        AND NEW.candidate_set_digest IS OLD.candidate_set_digest
        AND EXISTS (
            SELECT 1 FROM bestplan_verification_receipts AS r
            WHERE r.plan_id = OLD.plan_id
              AND r.authority_epoch IS OLD.proof_authority_epoch
              AND r.event_seq IS OLD.proof_event_seq
              AND r.event_hash IS OLD.proof_event_hash
              AND r.approval_digest IS OLD.approval_digest
              AND r.contract_digest IS OLD.promotion_contract_digest
              AND r.source_snapshot_digest IS OLD.source_snapshot_digest
              AND r.base_oid IS OLD.baseline_revision
              AND r.integration_oid IS OLD.integration_oid
              AND r.artifact_digest IS OLD.artifact_digest
              AND r.candidate_set_digest IS OLD.candidate_set_digest
              AND r.receipt_json IS NEW.verification_receipt_json
              AND r.receipt_digest IS NEW.verification_receipt_digest
              AND NEW.verified_at IS r.verified_at_ns / 1000000000.0
        )
        AND EXISTS (
            SELECT 1 FROM bestplan_proof_events AS e
            WHERE e.plan_id = OLD.plan_id
              AND e.stream = 'authority'
              AND e.origin = 'authority'
              AND e.kind = 'live_verified'
              AND e.phase = 'live_verified'
              AND e.authority_epoch IS OLD.proof_authority_epoch
              AND e.event_seq IS OLD.proof_event_seq
              AND e.event_hash IS OLD.proof_event_hash
              AND e.approval_digest IS OLD.approval_digest
              AND e.contract_digest IS OLD.promotion_contract_digest
              AND e.source_snapshot_digest IS OLD.source_snapshot_digest
              AND e.base_oid IS OLD.baseline_revision
              AND e.integration_oid IS OLD.integration_oid
              AND e.artifact_digest IS OLD.artifact_digest
              AND e.candidate_set_digest IS OLD.candidate_set_digest
              AND e.projected_state = 'completed_unverified'
        )
        AND EXISTS (
            SELECT 1 FROM bestplan_candidates AS c
            WHERE c.plan_id = OLD.plan_id
              AND c.base_oid IS OLD.baseline_revision
              AND c.approval_digest IS OLD.approval_digest
              AND c.contract_digest IS OLD.promotion_contract_digest
              AND c.source_snapshot_digest IS OLD.source_snapshot_digest
              AND c.receipt_json IS NOT NULL
              AND c.receipt_digest IS NOT NULL
        )
    ) THEN RAISE(ABORT, 'bestplan protocol-2 terminal receipt is incomplete') END;
END
""",
    "bestplan_plans_v2_projection_guard_v1": """
CREATE TRIGGER bestplan_plans_v2_projection_guard_v1
BEFORE UPDATE ON bestplan_plans
WHEN OLD.execution_protocol = 2
 AND NEW.state IS NOT 'completed_verified'
 AND (
       NEW.current_phase IS NOT OLD.current_phase
    OR NEW.integration_oid IS NOT OLD.integration_oid
    OR NEW.artifact_digest IS NOT OLD.artifact_digest
    OR NEW.proof_authority_epoch IS NOT OLD.proof_authority_epoch
    OR NEW.proof_event_seq IS NOT OLD.proof_event_seq
    OR NEW.proof_event_hash IS NOT OLD.proof_event_hash
    OR NEW.tests_verified_at IS NOT OLD.tests_verified_at
    OR NEW.review_verified_at IS NOT OLD.review_verified_at
    OR NEW.remote_verified_at IS NOT OLD.remote_verified_at
    OR NEW.live_verified_at IS NOT OLD.live_verified_at
    OR NEW.completed_at IS NOT OLD.completed_at
    OR (NEW.state IS NOT OLD.state AND (
           NEW.state IN ('completed_unverified', 'completed_verified')
        OR OLD.proof_event_seq IS NOT NULL
       ))
 )
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM bestplan_proof_events AS e
        WHERE e.plan_id = OLD.plan_id
          AND e.stream = 'authority'
          AND e.authority_epoch IS NEW.proof_authority_epoch
          AND e.event_seq IS NEW.proof_event_seq
          AND e.event_hash IS NEW.proof_event_hash
          AND e.previous_phase IS OLD.current_phase
          AND e.phase IS NEW.current_phase
          AND e.projected_state IS NEW.state
          AND e.integration_oid IS NEW.integration_oid
          AND e.artifact_digest IS NEW.artifact_digest
          AND e.candidate_set_digest IS NEW.candidate_set_digest
          AND NEW.tests_verified_at IS CASE
              WHEN e.phase = 'tests_verified' THEN e.created_at_ns / 1000000000.0
              ELSE OLD.tests_verified_at END
          AND NEW.review_verified_at IS CASE
              WHEN e.phase = 'review_verified' THEN e.created_at_ns / 1000000000.0
              ELSE OLD.review_verified_at END
          AND NEW.remote_verified_at IS CASE
              WHEN e.phase = 'remote_verified' THEN e.created_at_ns / 1000000000.0
              ELSE OLD.remote_verified_at END
          AND NEW.live_verified_at IS CASE
              WHEN e.phase = 'live_verified' THEN e.created_at_ns / 1000000000.0
              ELSE OLD.live_verified_at END
          AND NEW.completed_at IS CASE
              WHEN e.projected_state = 'completed_unverified'
              THEN COALESCE(OLD.completed_at, e.created_at_ns / 1000000000.0)
              ELSE OLD.completed_at END
    ) THEN RAISE(ABORT, 'bestplan protocol-2 projection requires its next event') END;
END
""",
    "bestplan_plans_v2_dispatch_guard_v1": """
CREATE TRIGGER bestplan_plans_v2_dispatch_guard_v1
BEFORE UPDATE ON bestplan_plans
WHEN OLD.execution_protocol = 2
 AND (
       NEW.dispatch_state IS NOT OLD.dispatch_state
    OR NEW.dispatch_id IS NOT OLD.dispatch_id
    OR NEW.resolved_runtime_json IS NOT OLD.resolved_runtime_json
    OR NEW.delegation_ids_json IS NOT OLD.delegation_ids_json
    OR NEW.sandbox_workspace IS NOT OLD.sandbox_workspace
    OR NEW.dispatch_owner IS NOT OLD.dispatch_owner
    OR (OLD.state IN ('pending','approved') AND NEW.state = 'running')
    OR (OLD.state = 'running' AND NEW.state = 'waiting')
 )
BEGIN
    SELECT CASE WHEN NOT (
        (
            OLD.state IN ('pending','approved')
            AND NEW.state = 'running'
            AND NEW.dispatch_state = 'intent'
            AND OLD.dispatch_id IS NULL
            AND NEW.dispatch_id = 'bestplan-' || OLD.plan_id
            AND NEW.delegation_ids_json = '["' || NEW.dispatch_id || '"]'
            AND OLD.resolved_runtime_json IS NULL
            AND length(NEW.resolved_runtime_json) BETWEEN 2 AND 32768
            AND NEW.approved_at IS NOT NULL
            AND NEW.approved_by IS NOT NULL
            AND NEW.started_at IS NOT NULL
            AND NEW.dispatch_updated_at IS NOT NULL
            AND NEW.sandbox_workspace IS OLD.sandbox_workspace
            AND NEW.dispatch_owner IS OLD.dispatch_owner
            AND NEW.error IS NULL
        )
        OR
        (
            OLD.state = 'running'
            AND NEW.state = 'running'
            AND OLD.dispatch_state IN ('intent','unknown')
            AND NEW.dispatch_state = 'dispatching'
            AND NEW.delegation_ids_json IS OLD.delegation_ids_json
            AND NEW.sandbox_workspace IS OLD.sandbox_workspace
            AND NEW.dispatch_owner LIKE 'pid:%'
            AND NEW.dispatch_id IS OLD.dispatch_id
            AND NEW.resolved_runtime_json IS OLD.resolved_runtime_json
            AND NEW.error IS OLD.error
        )
        OR EXISTS (
            SELECT 1 FROM bestplan_proof_events AS e
            WHERE e.plan_id = OLD.plan_id
              AND e.stream = 'advisory'
              AND e.event_seq = (
                  SELECT MAX(h.event_seq) FROM bestplan_proof_events AS h
                  WHERE h.plan_id = e.plan_id
                    AND h.stream = e.stream
                    AND h.authority_epoch = e.authority_epoch
              )
              AND e.payload_json IS NEW.evidence_json
              AND NEW.state IS OLD.state
              AND NEW.dispatch_state IS CASE
                  WHEN e.compatibility_dispatch_state IS NULL
                  THEN OLD.dispatch_state ELSE e.compatibility_dispatch_state END
              AND NEW.delegation_ids_json IS CASE
                  WHEN e.compatibility_delegation_ids_json IS NULL
                  THEN OLD.delegation_ids_json ELSE e.compatibility_delegation_ids_json END
              AND NEW.sandbox_workspace IS CASE
                  WHEN e.compatibility_sandbox_workspace IS NULL
                  THEN OLD.sandbox_workspace ELSE e.compatibility_sandbox_workspace END
              AND NEW.dispatch_owner IS CASE
                  WHEN e.compatibility_clear_dispatch_owner = 1
                  THEN NULL ELSE OLD.dispatch_owner END
              AND NEW.dispatch_id IS OLD.dispatch_id
              AND NEW.resolved_runtime_json IS OLD.resolved_runtime_json
        )
    ) THEN RAISE(ABORT, 'bestplan protocol-2 dispatch projection requires an advisory event') END;
END
""",
    "bestplan_plans_v2_compatibility_guard_v1": """
CREATE TRIGGER bestplan_plans_v2_compatibility_guard_v1
BEFORE UPDATE ON bestplan_plans
WHEN OLD.execution_protocol = 2
 AND (NEW.evidence_json IS NOT OLD.evidence_json OR NEW.error IS NOT OLD.error)
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM bestplan_proof_events AS e
        WHERE e.plan_id = OLD.plan_id
          AND (
              (
                  e.stream = 'authority'
                  AND e.authority_epoch IS NEW.proof_authority_epoch
                  AND e.event_seq IS NEW.proof_event_seq
                  AND e.event_hash IS NEW.proof_event_hash
              )
              OR (
                  e.stream = 'advisory'
                  AND e.event_seq = (
                      SELECT MAX(h.event_seq) FROM bestplan_proof_events AS h
                      WHERE h.plan_id = e.plan_id
                        AND h.stream = e.stream
                        AND h.authority_epoch = e.authority_epoch
                  )
              )
          )
          AND e.payload_json IS NEW.evidence_json
          AND (e.compatibility_error IS NEW.error OR NEW.error IS OLD.error)
    ) THEN RAISE(ABORT, 'bestplan protocol-2 compatibility projection requires an event') END;
END
""",
    "bestplan_plans_v2_receipt_guard_v1": """
CREATE TRIGGER bestplan_plans_v2_receipt_guard_v1
BEFORE UPDATE ON bestplan_plans
WHEN OLD.execution_protocol = 2
 AND (
       NEW.verification_receipt_json IS NOT OLD.verification_receipt_json
    OR NEW.verification_receipt_digest IS NOT OLD.verification_receipt_digest
    OR NEW.verified_at IS NOT OLD.verified_at
    OR (OLD.state = 'completed_verified' AND (
           NEW.state IS NOT OLD.state
        OR NEW.current_phase IS NOT OLD.current_phase
        OR NEW.integration_oid IS NOT OLD.integration_oid
        OR NEW.artifact_digest IS NOT OLD.artifact_digest
        OR NEW.proof_authority_epoch IS NOT OLD.proof_authority_epoch
        OR NEW.proof_event_seq IS NOT OLD.proof_event_seq
        OR NEW.proof_event_hash IS NOT OLD.proof_event_hash
        OR NEW.candidate_set_digest IS NOT OLD.candidate_set_digest
        OR NEW.tests_verified_at IS NOT OLD.tests_verified_at
        OR NEW.review_verified_at IS NOT OLD.review_verified_at
        OR NEW.remote_verified_at IS NOT OLD.remote_verified_at
        OR NEW.live_verified_at IS NOT OLD.live_verified_at
        OR NEW.completed_at IS NOT OLD.completed_at
    ))
 )
BEGIN
    SELECT CASE WHEN NOT (
        OLD.state = 'completed_unverified'
        AND NEW.state = 'completed_verified'
        AND OLD.verification_receipt_json IS NULL
        AND OLD.verification_receipt_digest IS NULL
        AND OLD.verified_at IS NULL
        AND NEW.verification_receipt_json IS NOT NULL
        AND NEW.verification_receipt_digest IS NOT NULL
        AND NEW.verified_at IS NOT NULL
    ) THEN RAISE(ABORT, 'bestplan protocol-2 terminal projection is immutable') END;
END
""",
    "bestplan_proof_authority_project_v1": """
CREATE TRIGGER bestplan_proof_authority_project_v1
AFTER INSERT ON bestplan_proof_events
WHEN NEW.stream = 'authority'
BEGIN
    UPDATE bestplan_plans SET
        state = NEW.projected_state,
        current_phase = NEW.phase,
        integration_oid = NEW.integration_oid,
        artifact_digest = NEW.artifact_digest,
        proof_authority_epoch = NEW.authority_epoch,
        proof_event_seq = NEW.event_seq,
        proof_event_hash = NEW.event_hash,
        evidence_json = NEW.payload_json,
        error = CASE WHEN NEW.compatibility_error IS NULL
                     THEN error ELSE NEW.compatibility_error END,
        tests_verified_at = CASE WHEN NEW.phase = 'tests_verified'
            THEN NEW.created_at_ns / 1000000000.0 ELSE tests_verified_at END,
        review_verified_at = CASE WHEN NEW.phase = 'review_verified'
            THEN NEW.created_at_ns / 1000000000.0 ELSE review_verified_at END,
        remote_verified_at = CASE WHEN NEW.phase = 'remote_verified'
            THEN NEW.created_at_ns / 1000000000.0 ELSE remote_verified_at END,
        live_verified_at = CASE WHEN NEW.phase = 'live_verified'
            THEN NEW.created_at_ns / 1000000000.0 ELSE live_verified_at END,
        completed_at = CASE WHEN NEW.projected_state = 'completed_unverified'
            THEN COALESCE(completed_at, NEW.created_at_ns / 1000000000.0)
            ELSE completed_at END
    WHERE plan_id = NEW.plan_id AND execution_protocol = 2;
END
""",
    "bestplan_proof_advisory_project_v1": """
CREATE TRIGGER bestplan_proof_advisory_project_v1
AFTER INSERT ON bestplan_proof_events
WHEN NEW.stream = 'advisory'
BEGIN
    UPDATE bestplan_plans SET
        evidence_json = NEW.payload_json,
        error = CASE WHEN NEW.compatibility_error IS NULL
                     THEN error ELSE NEW.compatibility_error END,
        dispatch_state = CASE WHEN NEW.compatibility_dispatch_state IS NULL
            THEN dispatch_state ELSE NEW.compatibility_dispatch_state END,
        delegation_ids_json = CASE
            WHEN NEW.compatibility_delegation_ids_json IS NULL
            THEN delegation_ids_json ELSE NEW.compatibility_delegation_ids_json END,
        sandbox_workspace = CASE
            WHEN NEW.compatibility_sandbox_workspace IS NULL
            THEN sandbox_workspace ELSE NEW.compatibility_sandbox_workspace END,
        dispatch_owner = CASE WHEN NEW.compatibility_clear_dispatch_owner = 1
            THEN NULL ELSE dispatch_owner END,
        dispatch_updated_at = NEW.created_at_ns / 1000000000.0
    WHERE plan_id = NEW.plan_id AND execution_protocol = 2;
END
""",
}


def _normalize_sql(value: str) -> str:
    # Fail closed on definition drift.  In particular, never case-fold or
    # rewrite whitespace inside quoted literals: SQLite compares those bytes
    # semantically, so 'authority' and 'AUTHORITY' are different guards.
    return value.strip().rstrip(";").rstrip()


def _ensure_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: Mapping[str, str],
) -> None:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    existing = {str(row[1]): str(row[2]).upper() for row in rows}
    for name, sql_type in columns.items():
        if name in existing:
            if existing[name] and existing[name] != sql_type:
                raise ProofMigrationError(f"{table}.{name} has an incompatible type")
            continue
        conn.execute(f'ALTER TABLE {table} ADD COLUMN "{name}" {sql_type}')


def _ensure_sql_object(
    conn: sqlite3.Connection,
    *,
    kind: str,
    name: str,
    sql: str,
) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type=? AND name=?",
        (kind, name),
    ).fetchone()
    if row is None:
        conn.execute(sql)
        return
    existing_sql = row[0]
    if not isinstance(existing_sql, str) or _normalize_sql(existing_sql) != _normalize_sql(sql):
        raise ProofMigrationError(f"existing {kind} {name} has an incompatible definition")


def install_proof_schema(conn: sqlite3.Connection) -> None:
    """Add the Task-3 schema inside the caller's existing write transaction."""

    for table, create_sql, columns in (
        ("bestplan_proof_events", _CREATE_EVENTS_SQL, _EVENT_COLUMNS),
        ("bestplan_candidates", _CREATE_CANDIDATES_SQL, _CANDIDATE_COLUMNS),
        (
            "bestplan_verification_receipts",
            _CREATE_VERIFICATIONS_SQL,
            _VERIFICATION_COLUMNS,
        ),
    ):
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists is None:
            conn.execute(create_sql)
        else:
            _ensure_columns(conn, table, columns)
    for name, sql in _INDEX_SQL.items():
        _ensure_sql_object(conn, kind="index", name=name, sql=sql)
    for name, sql in _TRIGGER_SQL.items():
        _ensure_sql_object(conn, kind="trigger", name=name, sql=sql)


def _event_body(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": EVENT_SCHEMA,
        "version": 1,
        "plan_id": values["plan_id"],
        "stream": values["stream"],
        "protocol": values["protocol"],
        "authority_epoch": values["authority_epoch"],
        "event_seq": values["event_seq"],
        "previous_hash": values["previous_hash"],
        "operation_id": values["operation_id"],
        "kind": values["kind"],
        "previous_phase": values["previous_phase"],
        "phase": values["phase"],
        "projected_state": values["projected_state"],
        "approval_digest": values["approval_digest"],
        "contract_digest": values["contract_digest"],
        "source_snapshot_digest": values["source_snapshot_digest"],
        "base_oid": values["base_oid"],
        "integration_oid": values["integration_oid"],
        "artifact_digest": values["artifact_digest"],
        "candidate_set_digest": values["candidate_set_digest"],
        "origin": values["origin"],
        "payload_digest": values["payload_digest"],
        "raw_output_sha256": values["raw_output_sha256"],
        "raw_output_kind": values["raw_output_kind"],
        "raw_output_framed_sha256": values["raw_output_framed_sha256"],
        "operation_fingerprint": values["operation_fingerprint"],
        "contract_digest_input": values["contract_digest_input"],
        "created_at_policy": values["created_at_policy"],
        "compatibility_error": values["compatibility_error"],
        "compatibility_dispatch_state": values["compatibility_dispatch_state"],
        "compatibility_delegation_ids_json": values[
            "compatibility_delegation_ids_json"
        ],
        "compatibility_sandbox_workspace": values[
            "compatibility_sandbox_workspace"
        ],
        "compatibility_clear_dispatch_owner": values[
            "compatibility_clear_dispatch_owner"
        ],
        "created_at_ns": values["created_at_ns"],
    }


def _event_hash(values: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        EVENT_HASH_DOMAIN + _canonical_json(_event_body(values)).encode("ascii")
    ).hexdigest()


def _operation_fingerprint(values: Mapping[str, Any]) -> str:
    body = {
        "schema": "hermes.bestplan.proof-operation.v1",
        "version": 1,
        "plan_id": values["plan_id"],
        "stream": values["stream"],
        "authority_epoch": values["authority_epoch"],
        "expected_epoch": values["expected_epoch"],
        "expected_seq": values["expected_seq"],
        "expected_hash": values["expected_hash"],
        "operation_id": values["operation_id"],
        "kind": values["kind"],
        "phase": values["phase"],
        "projected_state": values["projected_state"],
        "approval_digest": values["approval_digest"],
        "contract_digest": values["contract_digest"],
        "contract_digest_input": values["contract_digest_input"],
        "source_snapshot_digest": values["source_snapshot_digest"],
        "base_oid": values["base_oid"],
        "integration_oid": values["integration_oid"],
        "artifact_digest": values["artifact_digest"],
        "candidate_set_digest": values["candidate_set_digest"],
        "origin": values["origin"],
        "payload_json": values["payload_json"],
        "payload_digest": values["payload_digest"],
        "raw_output_sha256": values["raw_output_sha256"],
        "raw_output_kind": values["raw_output_kind"],
        "raw_output_framed_sha256": values["raw_output_framed_sha256"],
        "compatibility_error": values["compatibility_error"],
        "compatibility_dispatch_state": values["compatibility_dispatch_state"],
        "compatibility_delegation_ids_json": values[
            "compatibility_delegation_ids_json"
        ],
        "compatibility_sandbox_workspace": values[
            "compatibility_sandbox_workspace"
        ],
        "compatibility_clear_dispatch_owner": values[
            "compatibility_clear_dispatch_owner"
        ],
        "created_at_policy": values["created_at_policy"],
        "created_at_ns": (
            values["created_at_ns"]
            if values["created_at_policy"] == "explicit"
            else None
        ),
    }
    return hashlib.sha256(
        OPERATION_FINGERPRINT_DOMAIN + _canonical_json(body).encode("ascii")
    ).hexdigest()


def _candidate_set_digest(
    conn: sqlite3.Connection, plan_id: str, context: Mapping[str, Any]
) -> str:
    rows = conn.execute(
        """SELECT receipt_digest FROM bestplan_candidates
           WHERE plan_id=? ORDER BY candidate_id, attempt_id, receipt_digest""",
        (plan_id,),
    ).fetchall()
    if not rows:
        raise ProofValidationError("authority events require candidate receipts")
    body = {
        "schema": "hermes.bestplan.candidate-set.v1",
        "version": 1,
        "plan_id": plan_id,
        "base_oid": context["base_oid"],
        "approval_digest": context["approval_digest"],
        "contract_digest": context["contract_digest"],
        "source_snapshot_digest": context["source_snapshot_digest"],
        "receipt_digests": [str(row["receipt_digest"]) for row in rows],
    }
    return _digest(_canonical_json(body), CANDIDATE_SET_DIGEST_DOMAIN)


def _authority_receipt_body(
    *,
    contract: Mapping[str, Any],
    plan_id: str,
    authority_epoch: str,
    event_seq: int,
    event_hash: str,
    approval_digest: str,
    contract_digest: str,
    source_snapshot_digest: str,
    base_oid: str,
    integration_oid: str,
    artifact_digest: str,
    candidate_set_digest: str,
    issued_at_ns: int,
    expires_at_ns: int,
) -> dict[str, Any]:
    publication = contract["publication"]
    live = contract["live_target"]
    controller = contract["controller"]
    return {
        "schema": AUTHORITY_RECEIPT_SCHEMA,
        "version": 1,
        "kind": "live_verified",
        "plan_id": plan_id,
        "execution_protocol": 2,
        "authority_epoch": authority_epoch,
        "event_seq": event_seq,
        "event_hash": event_hash,
        "approval_digest": approval_digest,
        "promotion_contract_digest": contract_digest,
        "source_snapshot_digest": source_snapshot_digest,
        "candidate_set_digest": candidate_set_digest,
        "repository_id": contract["repository"]["repository_id"],
        "base_oid": base_oid,
        "integration_oid": integration_oid,
        "artifact_digest": artifact_digest,
        "local": {
            "ref": contract["source"]["local_ref"],
            "approved_oid": contract["source"]["local_main_oid"],
            "observed_oid": integration_oid,
        },
        "remote": {
            "name": publication["remote_name"],
            "identity_fingerprint": publication["remote_identity_fingerprint"],
            "ref": publication["remote_ref"],
            "approved_oid": publication["observed_oid"],
            "observed_oid": integration_oid,
        },
        "live": {
            "adapter": live["adapter"],
            "target_id": live["target_id"],
            "service": live["service"],
            "observed_release": integration_oid,
            "observed_artifact_digest": artifact_digest,
        },
        "controller": {
            "id": controller["controller_id"],
            "release_oid": controller["release_oid"],
            "artifact_sha256": controller["artifact_sha256"],
        },
        "issued_at_ns": issued_at_ns,
        "expires_at_ns": expires_at_ns,
    }


def _validate_authority_receipt_contract(
    row: Mapping[str, Any], verification: AuthorityVerification
) -> None:
    try:
        contract = validate_execution_contract(
            json.loads(str(row["promotion_contract_json"]))
        )
    except Exception as exc:
        raise ProofValidationError("authority receipt contract is invalid") from exc
    if contract.get("promotion_mode") != "auto_live":
        raise ProofValidationError("authority receipt contract is not auto_live")
    expected = _authority_receipt_body(
        contract=contract,
        plan_id=verification.plan_id,
        authority_epoch=verification.authority_epoch,
        event_seq=verification.event_seq,
        event_hash=verification.event_hash,
        approval_digest=verification.approval_digest,
        contract_digest=verification.contract_digest,
        source_snapshot_digest=verification.source_snapshot_digest,
        base_oid=str(row.get("baseline_revision")),
        integration_oid=verification.integration_oid,
        artifact_digest=verification.artifact_digest,
        candidate_set_digest=verification.candidate_set_digest,
        issued_at_ns=verification.issued_at_ns,
        expires_at_ns=verification.expires_at_ns,
    )
    if _canonical_json(expected) != verification.receipt_json:
        raise ProofValidationError("authority receipt contract binding differs")


def _require_exact_review_pass(
    connection: sqlite3.Connection,
    *,
    plan_row: Mapping[str, Any],
) -> None:
    """Require one current two-slot ReviewStore pass for this integration."""

    try:
        plan_id = _nonempty(plan_row.get("plan_id"), "plan_id")
        integration_oid = _oid(
            plan_row.get("integration_oid"), "integration_oid"
        )
        rows = connection.execute(
            """
            SELECT pass.review_receipt_digest, generation.target_json
            FROM review_pass_receipts AS pass
            JOIN review_jobs AS job ON job.job_id=pass.job_id
            JOIN review_generations AS generation
              ON generation.job_id=pass.job_id
             AND generation.generation=pass.generation
            WHERE job.source_kind='bestplan_integration'
              AND job.source_id=? AND job.state='passed'
              AND job.cancel_requested=0
              AND job.current_generation=pass.generation
              AND pass.integration_oid=?
              AND pass.fencing_token=job.fencing_token
              AND generation.state='passed'
            """,
            (plan_id, integration_oid),
        ).fetchall()
        if len(rows) != 1:
            raise ProofValidationError(
                "exact canonical review pass is required"
            )
        target_payload = json.loads(str(rows[0]["target_json"]))
        if not isinstance(target_payload, dict):
            raise ProofValidationError(
                "exact canonical review pass is required"
            )
        source_kind = target_payload.pop("source_kind", None)
        if source_kind != "bestplan_integration":
            raise ProofValidationError(
                "exact canonical review pass is required"
            )
        from agent.review_engine import ReviewStore, ReviewTarget

        target = ReviewTarget.bestplan_integration(**target_payload)
        if (
            target.plan_id != plan_id
            or target.base_oid != plan_row.get("baseline_revision")
            or target.local_target_oid != plan_row.get("baseline_revision")
            or target.integration_oid != integration_oid
            or target.approval_digest != plan_row.get("approval_digest")
            or target.contract_digest
            != plan_row.get("promotion_contract_digest")
        ):
            raise ProofValidationError(
                "exact canonical review pass is required"
            )
        ReviewStore.latest_exact_pass_in_transaction(
            connection,
            target=target,
            review_receipt_digest=str(rows[0]["review_receipt_digest"]),
        )
    except ProofValidationError:
        raise
    except Exception as exc:
        raise ProofValidationError(
            "exact canonical review pass is required"
        ) from exc


class ProofLedger:
    """Transactional append/read/verify interface over a ``BestplanStore``."""

    def __init__(self, store: Any):
        if not hasattr(store, "_execute_write") or not hasattr(store, "_connection"):
            raise ProofValidationError("store does not expose the BestPlan transaction API")
        self.store = store

    def append_event(
        self,
        *,
        plan_id: str,
        authority_epoch: str,
        operation_id: str,
        expected_epoch: str | None,
        expected_seq: int,
        expected_hash: str | None,
        kind: str,
        phase: str,
        projected_state: str,
        integration_oid: str | None,
        artifact_digest: str | None,
        origin: str,
        raw_output: Any,
        output_source: str,
        stream: str = "authority",
        contract_digest: str | None = None,
        compatibility_error: str | None = None,
        created_at_ns: int | None = None,
    ) -> ProofEventReceipt:
        normalized = self._normalize_append_inputs(
            plan_id=plan_id,
            stream=stream,
            authority_epoch=authority_epoch,
            operation_id=operation_id,
            expected_epoch=expected_epoch,
            expected_seq=expected_seq,
            expected_hash=expected_hash,
            kind=kind,
            phase=phase,
            projected_state=projected_state,
            integration_oid=integration_oid,
            artifact_digest=artifact_digest,
            origin=origin,
            contract_digest=contract_digest,
            compatibility_error=compatibility_error,
            created_at_ns=created_at_ns,
        )
        redacted = redact_output(raw_output, source=output_source)

        try:
            return self.store._execute_write(
                lambda conn: self._append_event_conn(
                    conn, redacted=redacted, **normalized
                )
            )
        except sqlite3.IntegrityError as exc:
            raise ProofValidationError(
                "proof event violated a relational invariant"
            ) from exc

    @staticmethod
    def _normalize_append_inputs(
        *,
        plan_id: str,
        stream: str,
        authority_epoch: str,
        operation_id: str,
        expected_epoch: str | None,
        expected_seq: int,
        expected_hash: str | None,
        kind: str,
        phase: str,
        projected_state: str,
        integration_oid: str | None,
        artifact_digest: str | None,
        origin: str,
        contract_digest: str | None,
        compatibility_error: str | None,
        created_at_ns: int | None,
        compatibility_dispatch_state: str | None = None,
        compatibility_delegation_ids_json: str | None = None,
        compatibility_sandbox_workspace: str | None = None,
        compatibility_clear_dispatch_owner: bool = False,
    ) -> dict[str, Any]:
        plan_id = _nonempty(plan_id, "plan_id")
        stream = _label(stream, "stream")
        if stream not in {"authority", "advisory"}:
            raise ProofValidationError("stream must be authority or advisory")
        authority_epoch = _nonempty(authority_epoch, "authority_epoch")
        operation_id = _operation_id(operation_id)
        expected_seq = _int(expected_seq, "expected_seq")
        if expected_seq == 0:
            if expected_epoch is not None or expected_hash is not None:
                raise ProofValidationError("empty expected head must use null epoch/hash")
        else:
            expected_epoch = _nonempty(expected_epoch, "expected_epoch")
            expected_hash = _sha256(expected_hash, "expected_hash")
        kind = _label(kind, "kind")
        phase = _label(phase, "phase")
        projected_state = _label(projected_state, "projected_state")
        origin = _label(origin, "origin")
        if stream == "authority":
            if origin not in {"promoter", "authority"}:
                raise ProofValidationError("authority event origin is unsupported")
            if phase == "live_verified" and origin != "authority":
                raise ProofValidationError("live event must be authority-origin")
        integration_oid = _oid(integration_oid, "integration_oid", allow_none=True)
        artifact_digest = _sha256(
            artifact_digest, "artifact_digest", allow_none=True
        )
        if contract_digest is not None:
            contract_digest = _sha256(contract_digest, "contract_digest")
        if compatibility_error is not None:
            compatibility_error = _label(
                compatibility_error, "compatibility_error"
            )
            if compatibility_error not in COMPATIBILITY_ERROR_CODES:
                raise ProofValidationError("unsupported compatibility error code")
        if compatibility_dispatch_state is not None:
            compatibility_dispatch_state = _label(
                compatibility_dispatch_state, "compatibility_dispatch_state"
            )
            if compatibility_dispatch_state not in {
                "intent",
                "dispatching",
                "scheduled",
                "unknown",
                "terminal",
            }:
                raise ProofValidationError("unsupported compatibility dispatch state")
        if compatibility_delegation_ids_json is not None:
            compatibility_delegation_ids_json = _nonempty(
                compatibility_delegation_ids_json,
                "compatibility_delegation_ids_json",
                maximum=1024,
            )
            if compatibility_delegation_ids_json != _canonical_json(
                [f"bestplan-{plan_id}"]
            ):
                raise ProofValidationError(
                    "compatibility delegation identity is not host-owned"
                )
        if compatibility_sandbox_workspace not in {None, ""}:
            raise ProofValidationError("compatibility sandbox projection must be empty")
        if type(compatibility_clear_dispatch_owner) is not bool:
            raise ProofValidationError("clear dispatch owner must be boolean")
        if created_at_ns is not None:
            created_at_ns = _int(created_at_ns, "created_at_ns")
        return {
            "plan_id": plan_id,
            "stream": stream,
            "authority_epoch": authority_epoch,
            "operation_id": operation_id,
            "expected_epoch": expected_epoch,
            "expected_seq": expected_seq,
            "expected_hash": expected_hash,
            "kind": kind,
            "phase": phase,
            "projected_state": projected_state,
            "integration_oid": integration_oid,
            "artifact_digest": artifact_digest,
            "origin": origin,
            "contract_digest": contract_digest,
            "compatibility_error": compatibility_error,
            "compatibility_dispatch_state": compatibility_dispatch_state,
            "compatibility_delegation_ids_json": compatibility_delegation_ids_json,
            "compatibility_sandbox_workspace": compatibility_sandbox_workspace,
            "compatibility_clear_dispatch_owner": int(
                compatibility_clear_dispatch_owner
            ),
            "created_at_ns": created_at_ns,
        }

    @staticmethod
    def _validate_event_receipt(event: ProofEventReceipt) -> None:
        _nonempty(event.plan_id, "plan_id")
        if event.stream not in {"authority", "advisory"}:
            raise ProofValidationError("proof event stream is unsupported")
        _nonempty(event.authority_epoch, "authority_epoch")
        _int(event.event_seq, "event_seq", minimum=1)
        _sha256(event.event_hash, "event_hash")
        _sha256(event.previous_hash, "previous_hash", allow_none=True)
        _operation_id(event.operation_id)
        if type(event.protocol) is not int or event.protocol != 2:
            raise ProofValidationError("proof event protocol is unsupported")
        for name in ("kind", "previous_phase", "phase", "projected_state", "origin"):
            _label(getattr(event, name), name)
        for name in (
            "approval_digest",
            "contract_digest",
            "source_snapshot_digest",
            "payload_digest",
            "raw_output_sha256",
            "raw_output_framed_sha256",
            "operation_fingerprint",
        ):
            _sha256(getattr(event, name), name)
        _oid(event.base_oid, "base_oid")
        _oid(event.integration_oid, "integration_oid", allow_none=True)
        _sha256(event.artifact_digest, "artifact_digest", allow_none=True)
        _sha256(
            event.candidate_set_digest,
            "candidate_set_digest",
            allow_none=True,
        )
        _sha256(
            event.contract_digest_input,
            "contract_digest_input",
            allow_none=True,
        )
        if event.raw_output_kind not in {
            "null",
            "boolean",
            "integer",
            "string",
            "bytes",
            "list",
            "mapping",
        }:
            raise ProofValidationError("proof event raw kind is unsupported")
        if event.created_at_policy not in {"clock", "explicit"}:
            raise ProofValidationError("proof event timestamp policy is unsupported")
        _int(event.created_at_ns, "created_at_ns")
        if (
            type(event.compatibility_clear_dispatch_owner) is not int
            or event.compatibility_clear_dispatch_owner not in {0, 1}
        ):
            raise ProofValidationError("proof event owner projection is invalid")
        if event.compatibility_dispatch_state is not None and (
            event.compatibility_dispatch_state
            not in {"intent", "dispatching", "scheduled", "unknown", "terminal"}
        ):
            raise ProofValidationError("proof event dispatch state is unsupported")
        if event.compatibility_sandbox_workspace not in {None, ""}:
            raise ProofValidationError("proof event sandbox projection is unsupported")
        try:
            payload = validate_redacted_projection(event.payload_json)
        except RedactionError:
            raise ProofValidationError(
                "proof event redacted payload is invalid or noncanonical"
            ) from None
        if _digest(event.payload_json, REDACTED_DIGEST_DOMAIN) != event.payload_digest:
            raise ProofValidationError("proof event payload digest differs")
        if (
            payload.get("raw_sha256") != event.raw_output_sha256
            or payload.get("raw_kind") != event.raw_output_kind
            or payload.get("raw_framed_sha256")
            != event.raw_output_framed_sha256
        ):
            raise ProofValidationError("proof event raw identity differs")
        if (
            event.compatibility_error is not None
            and event.compatibility_error not in COMPATIBILITY_ERROR_CODES
        ):
            raise ProofValidationError("proof event compatibility error is unsupported")
        if (
            event.compatibility_delegation_ids_json is not None
            and event.compatibility_delegation_ids_json
            != _canonical_json([f"bestplan-{event.plan_id}"])
        ):
            raise ProofValidationError(
                "proof event compatibility delegation identity differs"
            )
        if _event_hash(event.to_dict()) != event.event_hash:
            raise ProofValidationError("proof event hash differs")
        fingerprint_values = event.to_dict()
        fingerprint_values.update(
            {
                "expected_epoch": (
                    None if event.event_seq == 1 else event.authority_epoch
                ),
                "expected_seq": event.event_seq - 1,
                "expected_hash": event.previous_hash,
            }
        )
        if _operation_fingerprint(fingerprint_values) != event.operation_fingerprint:
            raise ProofValidationError("proof event operation fingerprint differs")

    def _append_event_conn(
        self,
        conn: sqlite3.Connection,
        *,
        redacted: RedactedOutput,
        plan_id: str,
        stream: str,
        authority_epoch: str,
        operation_id: str,
        expected_epoch: str | None,
        expected_seq: int,
        expected_hash: str | None,
        kind: str,
        phase: str,
        projected_state: str,
        integration_oid: str | None,
        artifact_digest: str | None,
        origin: str,
        contract_digest: str | None,
        compatibility_error: str | None,
        compatibility_dispatch_state: str | None,
        compatibility_delegation_ids_json: str | None,
        compatibility_sandbox_workspace: str | None,
        compatibility_clear_dispatch_owner: int,
        created_at_ns: int | None,
    ) -> ProofEventReceipt:
        # Lookup precedes the expected-head comparison so an exact retry can
        # recover its original receipt even after the stream advances.
        existing_row = conn.execute(
            """SELECT * FROM bestplan_proof_events
               WHERE plan_id=? AND stream=? AND authority_epoch=?
                 AND operation_id=?""",
            (plan_id, stream, authority_epoch, operation_id),
        ).fetchone()
        row = conn.execute(
            "SELECT * FROM bestplan_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None or int(row["execution_protocol"] or 1) != 2:
            raise ProofValidationError("proof events require a protocol-2 plan")
        values = dict(row)
        approval = values.get("approval_digest") or _ZERO_DIGEST
        stored_contract = values.get("promotion_contract_digest") or _ZERO_DIGEST
        source_digest = values.get("source_snapshot_digest") or _ZERO_DIGEST
        base_oid = values.get("baseline_revision") or ("0" * 40)
        candidate_set_digest = values.get("candidate_set_digest")
        if stream == "authority":
            candidates = conn.execute(
                "SELECT * FROM bestplan_candidates WHERE plan_id=? "
                "ORDER BY candidate_id, attempt_id, receipt_digest",
                (plan_id,),
            ).fetchall()
            for candidate_row in candidates:
                self._validate_candidate_receipt(
                    CandidateReceipt.from_row(candidate_row)
                )
            computed_candidate_set = _candidate_set_digest(
                conn,
                plan_id,
                {
                    "base_oid": base_oid,
                    "approval_digest": approval,
                    "contract_digest": stored_contract,
                    "source_snapshot_digest": source_digest,
                },
            )
            if candidate_set_digest is None:
                if phase != "candidate_ready":
                    raise ProofValidationError("candidate set was not frozen")
                changed = conn.execute(
                    "UPDATE bestplan_plans SET candidate_set_digest=? "
                    "WHERE plan_id=? AND candidate_set_digest IS NULL",
                    (computed_candidate_set, plan_id),
                ).rowcount
                if changed != 1:
                    raise ProofValidationError("candidate set freeze did not match")
                candidate_set_digest = computed_candidate_set
                values["candidate_set_digest"] = computed_candidate_set
            elif candidate_set_digest != computed_candidate_set:
                raise ProofValidationError("candidate set digest differs from receipts")
        if contract_digest is not None and stored_contract != contract_digest:
            raise ProofValidationError("event contract digest differs from plan")
        timestamp_policy = "clock" if created_at_ns is None else "explicit"
        effective_created_at_ns = (
            int(existing_row["created_at_ns"])
            if existing_row is not None and created_at_ns is None
            else time.time_ns() if created_at_ns is None else created_at_ns
        )
        fingerprint_values = {
            "plan_id": plan_id,
            "stream": stream,
            "authority_epoch": authority_epoch,
            "expected_epoch": expected_epoch,
            "expected_seq": expected_seq,
            "expected_hash": expected_hash,
            "operation_id": operation_id,
            "kind": kind,
            "phase": phase,
            "projected_state": projected_state,
            "approval_digest": approval,
            "contract_digest": stored_contract,
            "contract_digest_input": contract_digest,
            "source_snapshot_digest": source_digest,
            "base_oid": base_oid,
            "integration_oid": integration_oid,
            "artifact_digest": artifact_digest,
            "candidate_set_digest": candidate_set_digest,
            "origin": origin,
            "payload_json": redacted.canonical_json,
            "payload_digest": redacted.summary_sha256,
            "raw_output_sha256": redacted.raw_sha256,
            "raw_output_kind": redacted.raw_kind,
            "raw_output_framed_sha256": redacted.raw_framed_sha256,
            "compatibility_error": compatibility_error,
            "compatibility_dispatch_state": compatibility_dispatch_state,
            "compatibility_delegation_ids_json": compatibility_delegation_ids_json,
            "compatibility_sandbox_workspace": compatibility_sandbox_workspace,
            "compatibility_clear_dispatch_owner": compatibility_clear_dispatch_owner,
            "created_at_policy": timestamp_policy,
            "created_at_ns": effective_created_at_ns,
        }
        operation_fingerprint = _operation_fingerprint(fingerprint_values)
        if existing_row is not None:
            existing = ProofEventReceipt.from_row(existing_row)
            try:
                self._validate_event_receipt(existing)
            except ProofValidationError as exc:
                raise ProofValidationError(
                    "stored proof event failed integrity validation"
                ) from exc
            if existing.operation_fingerprint != operation_fingerprint:
                raise ProofOperationConflict(
                    "operation_id was reused with different canonical input"
                )
            return existing

        if stream == "authority":
            if _ZERO_DIGEST in {approval, stored_contract, source_digest}:
                raise ProofValidationError(
                    "authority events require complete approval/contract/source bindings"
                )
            from agent.bestplan_state import _validate_stored_plan_row

            try:
                _validate_stored_plan_row(row)
            except Exception as exc:
                raise ProofValidationError(
                    "authority event plan revalidation failed"
                ) from exc
            head_epoch = values.get("proof_authority_epoch")
            head_seq = int(values.get("proof_event_seq") or 0)
            head_hash = values.get("proof_event_hash")
            if (head_epoch, head_seq, head_hash) != (
                expected_epoch,
                expected_seq,
                expected_hash,
            ):
                raise ProofHeadMismatch("expected proof head differs from stored head")
            if values.get("state") != "running":
                raise ProofValidationError(
                    "authority milestones require a running plan state"
                )
            if head_seq:
                head_event = conn.execute(
                    """SELECT created_at_ns FROM bestplan_proof_events
                       WHERE plan_id=? AND stream='authority'
                         AND authority_epoch=? AND event_seq=? AND event_hash=?""",
                    (plan_id, head_epoch, head_seq, head_hash),
                ).fetchone()
                if (
                    head_event is None
                    or effective_created_at_ns < int(head_event["created_at_ns"])
                ):
                    raise ProofValidationError(
                        "authority event timestamp would regress"
                    )
            previous_phase = str(values.get("current_phase") or "")
            if (previous_phase, phase) not in AUTHORITY_PHASE_EDGES:
                raise ProofValidationError("invalid bestplan authority phase edge")
            if kind != phase:
                raise ProofValidationError("authority event kind must equal its phase")
            if phase == "live_verified":
                if projected_state != "completed_unverified":
                    raise ProofValidationError(
                        "live verification must project completed_unverified"
                    )
            elif projected_state != "running":
                raise ProofValidationError(
                    "authority milestone cannot change the plan state"
                )
            if phase == "review_verified":
                review_values = dict(values)
                review_values["integration_oid"] = integration_oid
                _require_exact_review_pass(
                    conn,
                    plan_row=review_values,
                )
        else:
            previous_phase = str(values.get("current_phase") or "captured")
            head = conn.execute(
                """SELECT authority_epoch, event_seq, event_hash
                   FROM bestplan_proof_events
                   WHERE plan_id=? AND stream='advisory' AND authority_epoch=?
                   ORDER BY event_seq DESC LIMIT 1""",
                (plan_id, authority_epoch),
            ).fetchone()
            head_epoch = authority_epoch if head is not None else None
            head_seq = int(head["event_seq"]) if head is not None else 0
            head_hash = head["event_hash"] if head is not None else None
            if (head_epoch, head_seq, head_hash) != (
                expected_epoch,
                expected_seq,
                expected_hash,
            ):
                raise ProofHeadMismatch("expected proof head differs from stored head")
            if phase != values.get("current_phase"):
                raise ProofValidationError(
                    "advisory events cannot change the authority phase"
                )
            if projected_state != values.get("state"):
                raise ProofValidationError(
                    "advisory events cannot change the plan state"
                )
        stored_integration = values.get("integration_oid")
        stored_artifact = values.get("artifact_digest")
        if stored_integration is not None and integration_oid != stored_integration:
            raise ProofValidationError("integration identity would regress")
        if stored_artifact is not None and artifact_digest != stored_artifact:
            raise ProofValidationError("artifact identity would regress")
        if stream == "authority":
            if phase in {"candidate_ready", "queued", "integrating"}:
                if integration_oid is not None or artifact_digest is not None:
                    raise ProofValidationError(
                        "pre-integration phases cannot bind integration/artifact identities"
                    )
            elif phase in {
                "integrated_proven",
                "testing",
                "tests_verified",
                "reviewing",
                "review_verified",
            }:
                if integration_oid is None or artifact_digest is not None:
                    raise ProofValidationError(
                        "pre-artifact phases require only the integration identity"
                    )
            elif integration_oid is None or artifact_digest is None:
                raise ProofValidationError(
                    "artifact and later phases require both frozen identities"
                )
        event_values = {
            "plan_id": plan_id,
            "stream": stream,
            "authority_epoch": authority_epoch,
            "event_seq": head_seq + 1,
            "previous_hash": head_hash,
            "operation_id": operation_id,
            "protocol": 2,
            "kind": kind,
            "previous_phase": previous_phase,
            "phase": phase,
            "projected_state": projected_state,
            "approval_digest": approval,
            "contract_digest": stored_contract,
            "source_snapshot_digest": source_digest,
            "base_oid": base_oid,
            "integration_oid": integration_oid,
            "artifact_digest": artifact_digest,
            "candidate_set_digest": candidate_set_digest,
            "origin": origin,
            "payload_json": redacted.canonical_json,
            "payload_digest": redacted.summary_sha256,
            "raw_output_sha256": redacted.raw_sha256,
            "raw_output_kind": redacted.raw_kind,
            "raw_output_framed_sha256": redacted.raw_framed_sha256,
            "operation_fingerprint": operation_fingerprint,
            "contract_digest_input": contract_digest,
            "created_at_policy": timestamp_policy,
            "compatibility_error": compatibility_error,
            "compatibility_dispatch_state": compatibility_dispatch_state,
            "compatibility_delegation_ids_json": compatibility_delegation_ids_json,
            "compatibility_sandbox_workspace": compatibility_sandbox_workspace,
            "compatibility_clear_dispatch_owner": compatibility_clear_dispatch_owner,
            "created_at_ns": effective_created_at_ns,
        }
        event_values["event_hash"] = _event_hash(event_values)
        columns = tuple(event_values)
        conn.execute(
            f"INSERT INTO bestplan_proof_events ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            tuple(event_values[name] for name in columns),
        )
        inserted = conn.execute(
            """SELECT * FROM bestplan_proof_events
               WHERE plan_id=? AND stream=? AND authority_epoch=? AND event_seq=?""",
            (plan_id, stream, authority_epoch, event_values["event_seq"]),
        ).fetchone()
        receipt = ProofEventReceipt.from_row(inserted)
        self._validate_event_receipt(receipt)
        return receipt

    def append_advisory(
        self,
        *,
        plan_id: str,
        operation_id: str,
        kind: str,
        raw_output: Any,
        output_source: str,
        origin: str = "gateway",
        compatibility_error: str | None = None,
        compatibility_dispatch_state: str | None = None,
        compatibility_delegation_ids_json: str | None = None,
        compatibility_sandbox_workspace: str | None = None,
        compatibility_clear_dispatch_owner: bool = False,
    ) -> ProofEventReceipt:
        try:
            return self.store._execute_write(
                lambda conn: self.append_advisory_in_transaction(
                    conn,
                    plan_id=plan_id,
                    operation_id=operation_id,
                    kind=kind,
                    raw_output=raw_output,
                    output_source=output_source,
                    origin=origin,
                    compatibility_error=compatibility_error,
                    compatibility_dispatch_state=compatibility_dispatch_state,
                    compatibility_delegation_ids_json=compatibility_delegation_ids_json,
                    compatibility_sandbox_workspace=compatibility_sandbox_workspace,
                    compatibility_clear_dispatch_owner=compatibility_clear_dispatch_owner,
                )
            )
        except sqlite3.IntegrityError as exc:
            raise ProofValidationError(
                "advisory event violated a relational invariant"
            ) from exc

    def append_advisory_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        plan_id: str,
        kind: str,
        raw_output: Any,
        output_source: str,
        operation_id: str | None = None,
        origin: str = "gateway",
        compatibility_error: str | None = None,
        compatibility_dispatch_state: str | None = None,
        compatibility_delegation_ids_json: str | None = None,
        compatibility_sandbox_workspace: str | None = None,
        compatibility_clear_dispatch_owner: bool = False,
    ) -> ProofEventReceipt:
        """Append one advisory using the caller's active write transaction.

        This is the single raw ingress used by state reconciliation; it avoids
        a nested ``BEGIN IMMEDIATE`` while preserving one-pass redaction.
        """

        plan_id = _nonempty(plan_id, "plan_id")
        row = conn.execute(
            "SELECT * FROM bestplan_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None or int(row["execution_protocol"] or 1) != 2:
            raise ProofValidationError("advisory events require a protocol-2 plan")
        values = dict(row)
        redacted = redact_output(raw_output, source=output_source)
        if operation_id is None:
            operation_name = _canonical_json(
                {
                    "plan_id": plan_id,
                    "kind": kind,
                    "origin": origin,
                    "raw_framed_sha256": redacted.raw_framed_sha256,
                    "phase": values.get("current_phase"),
                    "state": values.get("state"),
                    "proof_event_hash": values.get("proof_event_hash"),
                }
            )
            operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, operation_name))
        existing = conn.execute(
            """SELECT event_seq, previous_hash FROM bestplan_proof_events
               WHERE plan_id=? AND stream='advisory' AND authority_epoch='local'
                 AND operation_id=?""",
            (plan_id, operation_id),
        ).fetchone()
        head = conn.execute(
            """SELECT event_seq, event_hash FROM bestplan_proof_events
               WHERE plan_id=? AND stream='advisory' AND authority_epoch='local'
               ORDER BY event_seq DESC LIMIT 1""",
            (plan_id,),
        ).fetchone()
        if existing is not None:
            expected_seq = int(existing["event_seq"]) - 1
            expected_hash = existing["previous_hash"]
        else:
            expected_seq = 0 if head is None else int(head["event_seq"])
            expected_hash = None if head is None else str(head["event_hash"])
        normalized = self._normalize_append_inputs(
            plan_id=plan_id,
            stream="advisory",
            authority_epoch="local",
            operation_id=operation_id,
            expected_epoch=None if expected_seq == 0 else "local",
            expected_seq=expected_seq,
            expected_hash=expected_hash,
            kind=kind,
            phase=str(values.get("current_phase") or "captured"),
            projected_state=str(values.get("state")),
            integration_oid=values.get("integration_oid"),
            artifact_digest=values.get("artifact_digest"),
            origin=origin,
            contract_digest=None,
            compatibility_error=compatibility_error,
            created_at_ns=None,
            compatibility_dispatch_state=compatibility_dispatch_state,
            compatibility_delegation_ids_json=compatibility_delegation_ids_json,
            compatibility_sandbox_workspace=compatibility_sandbox_workspace,
            compatibility_clear_dispatch_owner=compatibility_clear_dispatch_owner,
        )
        return self._append_event_conn(conn, redacted=redacted, **normalized)

    def read_events(self, plan_id: str) -> list[ProofEventReceipt]:
        with self.store._read_lock():
            rows = self.store._connection().execute(
                """SELECT * FROM bestplan_proof_events WHERE plan_id=?
                   ORDER BY CASE stream WHEN 'authority' THEN 0 ELSE 1 END,
                            authority_epoch, event_seq""",
                (str(plan_id),),
            ).fetchall()
        return [ProofEventReceipt.from_row(row) for row in rows]

    def verify_chain(self, plan_id: str) -> bool:
        plan_id = _nonempty(plan_id, "plan_id")
        with self.store._read_lock():
            conn = self.store._connection()
            started_transaction = not conn.in_transaction
            if started_transaction:
                conn.execute("BEGIN")
            try:
                return self._verify_chain_snapshot(conn, plan_id)
            finally:
                if started_transaction and conn.in_transaction:
                    conn.rollback()

    def _verify_chain_snapshot(
        self,
        conn: sqlite3.Connection,
        plan_id: str,
    ) -> bool:
        rows = conn.execute(
            """SELECT * FROM bestplan_proof_events WHERE plan_id=?
               ORDER BY CASE stream WHEN 'authority' THEN 0 ELSE 1 END,
                        authority_epoch, event_seq""",
            (plan_id,),
        ).fetchall()
        events = [ProofEventReceipt.from_row(row) for row in rows]
        authority_epochs = {
            event.authority_epoch
            for event in events
            if event.stream == "authority"
        }
        if len(authority_epochs) > 1:
            raise ProofValidationError("proof chain has more than one authority epoch")
        groups: dict[tuple[str, str], list[ProofEventReceipt]] = {}
        for event in events:
            groups.setdefault((event.stream, event.authority_epoch), []).append(event)
        for group in groups.values():
            previous_hash = None
            for expected_seq, event in enumerate(group, start=1):
                if event.event_seq != expected_seq or event.previous_hash != previous_hash:
                    raise ProofValidationError("proof event sequence/hash link differs")
                self._validate_event_receipt(event)
                previous_hash = event.event_hash
        row = conn.execute(
            "SELECT * FROM bestplan_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise ProofValidationError("proof plan does not exist")
        from agent.bestplan_state import _validate_stored_plan_row

        try:
            validated_plan = _validate_stored_plan_row(row)
        except Exception:
            raise ProofValidationError(
                "proof plan immutable inputs failed revalidation"
            ) from None
        if validated_plan.execution_protocol != 2:
            raise ProofValidationError("proof verification requires protocol 2")
        values = dict(row)
        candidate_rows = conn.execute(
            """SELECT * FROM bestplan_candidates WHERE plan_id=?
               ORDER BY candidate_id, attempt_id, receipt_digest""",
            (plan_id,),
        ).fetchall()
        for candidate_row in candidate_rows:
            self._validate_candidate_receipt(
                CandidateReceipt.from_row(candidate_row)
            )
        authority = [event for event in events if event.stream == "authority"]
        advisory = [event for event in events if event.stream == "advisory"]
        previous_phase = "captured"
        previous_integration = None
        previous_artifact = None
        frozen_candidate_set = None
        previous_created_at_ns = None
        plan_bindings = {
            "approval_digest": values.get("approval_digest"),
            "contract_digest": values.get("promotion_contract_digest"),
            "source_snapshot_digest": values.get("source_snapshot_digest"),
            "base_oid": values.get("baseline_revision"),
        }
        local_terminal_overlay = _validate_local_terminal_overlay(
            values, validated_plan, authority, advisory,
        )
        if not authority and not local_terminal_overlay:
            initial_states = {
                "provisional",
                "pending",
                "approved",
                "rejected",
                "running",
                "waiting",
            }
            authority_projection_fields = (
                "integration_oid",
                "artifact_digest",
                "candidate_set_digest",
                "proof_authority_epoch",
                "proof_event_seq",
                "proof_event_hash",
                "verification_receipt_json",
                "verification_receipt_digest",
                "tests_verified_at",
                "review_verified_at",
                "remote_verified_at",
                "live_verified_at",
                "completed_at",
                "verified_at",
            )
            if (
                values.get("state") not in initial_states
                or values.get("current_phase") != "captured"
                or any(
                    values.get(name) is not None
                    for name in authority_projection_fields
                )
            ):
                raise ProofValidationError(
                    "authority-owned projection has no authority chain"
                )
        for event in authority:
            if event.origin not in {"promoter", "authority"}:
                raise ProofValidationError("authority event origin is unsupported")
            if event.phase == "live_verified" and event.origin != "authority":
                raise ProofValidationError("live event is not authority-origin")
            if event.kind != event.phase:
                raise ProofValidationError("authority event kind differs from phase")
            if any(
                getattr(event, name) != expected
                for name, expected in plan_bindings.items()
            ):
                raise ProofValidationError("authority event plan bindings differ")
            expected_state = (
                "completed_unverified"
                if event.phase == "live_verified"
                else "running"
            )
            if event.projected_state != expected_state:
                raise ProofValidationError("authority event state projection differs")
            if event.previous_phase != previous_phase:
                raise ProofValidationError("authority event previous phase differs")
            if (event.previous_phase, event.phase) not in AUTHORITY_PHASE_EDGES:
                raise ProofValidationError("invalid bestplan authority phase edge")
            if (
                previous_created_at_ns is not None
                and event.created_at_ns < previous_created_at_ns
            ):
                raise ProofValidationError("authority event timestamps regressed")
            if previous_integration is not None and event.integration_oid != previous_integration:
                raise ProofValidationError("authority integration identity regressed")
            if previous_artifact is not None and event.artifact_digest != previous_artifact:
                raise ProofValidationError("authority artifact identity regressed")
            if frozen_candidate_set is None:
                frozen_candidate_set = event.candidate_set_digest
            elif event.candidate_set_digest != frozen_candidate_set:
                raise ProofValidationError("authority candidate set digest changed")
            if event.phase in {"candidate_ready", "queued", "integrating"}:
                if event.integration_oid is not None or event.artifact_digest is not None:
                    raise ProofValidationError("pre-integration identity timing differs")
            elif event.phase in {
                "integrated_proven",
                "testing",
                "tests_verified",
                "reviewing",
                "review_verified",
            }:
                if event.integration_oid is None or event.artifact_digest is not None:
                    raise ProofValidationError("pre-artifact identity timing differs")
            elif event.integration_oid is None or event.artifact_digest is None:
                raise ProofValidationError("frozen identity timing differs")
            previous_phase = event.phase
            previous_integration = event.integration_oid
            previous_artifact = event.artifact_digest
            previous_created_at_ns = event.created_at_ns
        if authority:
            if not candidate_rows:
                raise ProofValidationError("authority chain has no candidate receipts")
            for candidate_row in candidate_rows:
                candidate = CandidateReceipt.from_row(candidate_row)
                if (
                    candidate.base_oid != plan_bindings["base_oid"]
                    or candidate.approval_digest
                    != plan_bindings["approval_digest"]
                    or candidate.contract_digest
                    != plan_bindings["contract_digest"]
                    or candidate.source_snapshot_digest
                    != plan_bindings["source_snapshot_digest"]
                ):
                    raise ProofValidationError(
                        "candidate receipt plan bindings differ"
                    )
            computed_candidate_set = _candidate_set_digest(
                conn,
                plan_id,
                {
                    "base_oid": plan_bindings["base_oid"],
                    "approval_digest": plan_bindings["approval_digest"],
                    "contract_digest": plan_bindings["contract_digest"],
                    "source_snapshot_digest": plan_bindings[
                        "source_snapshot_digest"
                    ],
                },
            )
            if (
                values.get("candidate_set_digest") != computed_candidate_set
                or frozen_candidate_set != computed_candidate_set
            ):
                raise ProofValidationError("authority candidate aggregate differs")
            final = authority[-1]
            terminal_overlay = (
                values.get("state") == "completed_verified"
                and final.projected_state == "completed_unverified"
                and final.phase == "live_verified"
            )
            if (
                values.get("proof_authority_epoch") != final.authority_epoch
                or values.get("proof_event_seq") != final.event_seq
                or values.get("proof_event_hash") != final.event_hash
                or values.get("current_phase") != final.phase
                or (
                    values.get("state") != final.projected_state
                    and not terminal_overlay
                )
                or values.get("integration_oid") != final.integration_oid
                or values.get("artifact_digest") != final.artifact_digest
                or values.get("candidate_set_digest")
                != final.candidate_set_digest
            ):
                raise ProofValidationError("plan projection differs from final event")
            if terminal_overlay:
                verification = self._verification_from_completed_row(values)
                self._validate_completed_snapshot(
                    values,
                    verification,
                    conn=conn,
                )
            elif values.get("state") == "completed_verified":
                raise ProofValidationError("completed plan has no terminal overlay")
            milestone_fields = {
                "tests_verified": "tests_verified_at",
                "review_verified": "review_verified_at",
                "remote_verified": "remote_verified_at",
                "live_verified": "live_verified_at",
            }
            for phase, field in milestone_fields.items():
                phase_event = next(
                    (event for event in authority if event.phase == phase),
                    None,
                )
                expected_time = (
                    None
                    if phase_event is None
                    else phase_event.created_at_ns / 1_000_000_000.0
                )
                if values.get(field) != expected_time:
                    raise ProofValidationError(
                        "plan milestone timestamp differs from authority event"
                    )
            expected_completed_at = (
                final.created_at_ns / 1_000_000_000.0
                if final.phase == "live_verified"
                else None
            )
            if values.get("completed_at") != expected_completed_at:
                raise ProofValidationError(
                    "plan completion timestamp differs from authority event"
                )
        if len({event.authority_epoch for event in advisory}) > 1:
            raise ProofValidationError("proof chain has more than one advisory epoch")
        evidence_json = values.get("evidence_json")
        current_authority_evidence = bool(
            authority and authority[-1].payload_json == evidence_json
        )
        current_advisory_evidence = bool(
            advisory and advisory[-1].payload_json == evidence_json
        )
        if evidence_json is not None and not (
            current_authority_evidence or current_advisory_evidence
        ):
            raise ProofValidationError(
                "compatibility evidence differs from current authority/advisory projection"
            )
        if advisory:
            latest = advisory[-1]
            for event_field, plan_field in (
                ("compatibility_error", "error"),
                ("compatibility_dispatch_state", "dispatch_state"),
                (
                    "compatibility_delegation_ids_json",
                    "delegation_ids_json",
                ),
                (
                    "compatibility_sandbox_workspace",
                    "sandbox_workspace",
                ),
            ):
                expected = next(
                    (
                        getattr(event, event_field)
                        for event in reversed(advisory)
                        if getattr(event, event_field) is not None
                    ),
                    None,
                )
                if expected is not None and values.get(plan_field) != expected:
                    raise ProofValidationError(
                        "plan advisory compatibility projection differs"
                    )
            if (
                latest.compatibility_clear_dispatch_owner
                and values.get("dispatch_owner") is not None
            ):
                raise ProofValidationError(
                    "plan advisory owner projection differs"
                )
        return True

    def record_candidate(
        self,
        *,
        plan_id: str,
        candidate_id: str,
        slice_id: str,
        attempt_id: str,
        commit_oid: str,
        tree_oid: str,
        raw_receipt: Any,
        created_at_ns: int | None = None,
    ) -> CandidateReceipt:
        plan_id = _nonempty(plan_id, "plan_id")
        candidate_id = _nonempty(candidate_id, "candidate_id")
        slice_id = _nonempty(slice_id, "slice_id")
        attempt_id = _nonempty(attempt_id, "attempt_id")
        commit_oid = _oid(commit_oid, "commit_oid")
        tree_oid = _oid(tree_oid, "tree_oid")
        if created_at_ns is not None:
            created_at_ns = _int(created_at_ns, "created_at_ns")
        redacted = redact_output(raw_receipt, source="candidate")

        def insert(conn: sqlite3.Connection):
            existing = conn.execute(
                """SELECT * FROM bestplan_candidates
                   WHERE plan_id=? AND (candidate_id=? OR attempt_id=?)""",
                (plan_id, candidate_id, attempt_id),
            ).fetchone()
            plan = conn.execute(
                "SELECT * FROM bestplan_plans WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            if plan is None or int(plan["execution_protocol"] or 1) != 2:
                raise ProofValidationError("candidate receipt requires protocol 2")
            plan_values = dict(plan)
            context = {
                "base_oid": plan_values.get("baseline_revision"),
                "approval_digest": plan_values.get("approval_digest"),
                "contract_digest": plan_values.get("promotion_contract_digest"),
                "source_snapshot_digest": plan_values.get("source_snapshot_digest"),
            }
            if not _oid(context["base_oid"], "base_oid"):
                raise ProofValidationError("candidate receipt has no captured base")
            for name in (
                "approval_digest",
                "contract_digest",
                "source_snapshot_digest",
            ):
                _sha256(context[name], name)
            timestamp_policy = "clock" if created_at_ns is None else "explicit"
            effective_created_at_ns = (
                int(existing["created_at_ns"])
                if existing is not None and created_at_ns is None
                else time.time_ns() if created_at_ns is None else created_at_ns
            )
            body = {
                "schema": CANDIDATE_SCHEMA,
                "version": 1,
                "plan_id": plan_id,
                "candidate_id": candidate_id,
                "slice_id": slice_id,
                "attempt_id": attempt_id,
                "commit_oid": commit_oid,
                "tree_oid": tree_oid,
                **context,
                "output": json.loads(redacted.canonical_json),
                "created_at_policy": timestamp_policy,
                "created_at_ns": effective_created_at_ns,
            }
            receipt_json = _canonical_json(body)
            receipt_digest = _digest(receipt_json, CANDIDATE_DIGEST_DOMAIN)
            if existing is not None:
                candidate = CandidateReceipt.from_row(existing)
                self._validate_candidate_receipt(candidate)
                expected = (
                    candidate_id,
                    slice_id,
                    attempt_id,
                    commit_oid,
                    tree_oid,
                    context["base_oid"],
                    context["approval_digest"],
                    context["contract_digest"],
                    context["source_snapshot_digest"],
                    receipt_json,
                    receipt_digest,
                    redacted.raw_sha256,
                    redacted.raw_kind,
                    redacted.raw_framed_sha256,
                    timestamp_policy,
                    effective_created_at_ns,
                )
                actual = tuple(
                    getattr(candidate, name)
                    for name in (
                        "candidate_id",
                        "slice_id",
                        "attempt_id",
                        "commit_oid",
                        "tree_oid",
                        "base_oid",
                        "approval_digest",
                        "contract_digest",
                        "source_snapshot_digest",
                        "receipt_json",
                        "receipt_digest",
                        "raw_output_sha256",
                        "raw_output_kind",
                        "raw_output_framed_sha256",
                        "created_at_policy",
                        "created_at_ns",
                    )
                )
                if actual != expected:
                    raise ProofOperationConflict(
                        "candidate identity was reused with different input"
                    )
                return candidate
            values = (
                plan_id,
                candidate_id,
                slice_id,
                attempt_id,
                commit_oid,
                tree_oid,
                context["base_oid"],
                context["approval_digest"],
                context["contract_digest"],
                context["source_snapshot_digest"],
                receipt_json,
                receipt_digest,
                redacted.raw_sha256,
                redacted.raw_kind,
                redacted.raw_framed_sha256,
                timestamp_policy,
                effective_created_at_ns,
            )
            conn.execute(
                """INSERT INTO bestplan_candidates (
                    plan_id,candidate_id,slice_id,attempt_id,commit_oid,tree_oid,
                    base_oid,approval_digest,contract_digest,source_snapshot_digest,
                    receipt_json,receipt_digest,raw_output_sha256,raw_output_kind,
                    raw_output_framed_sha256,created_at_policy,created_at_ns
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                values,
            )
            inserted = conn.execute(
                "SELECT * FROM bestplan_candidates WHERE plan_id=? AND candidate_id=?",
                (plan_id, candidate_id),
            ).fetchone()
            receipt = CandidateReceipt.from_row(inserted)
            self._validate_candidate_receipt(receipt)
            return receipt

        try:
            return self.store._execute_write(insert)
        except sqlite3.IntegrityError as exc:
            raise ProofValidationError("candidate receipt violated an invariant") from exc

    @staticmethod
    def _validate_candidate_receipt(candidate: CandidateReceipt) -> None:
        for name in ("plan_id", "candidate_id", "slice_id", "attempt_id"):
            _nonempty(getattr(candidate, name), name)
        for name in ("commit_oid", "tree_oid", "base_oid"):
            _oid(getattr(candidate, name), name)
        for name in (
            "approval_digest",
            "contract_digest",
            "source_snapshot_digest",
            "receipt_digest",
            "raw_output_sha256",
            "raw_output_framed_sha256",
        ):
            _sha256(getattr(candidate, name), name)
        _nonempty(candidate.receipt_json, "receipt_json", maximum=32768)
        if candidate.raw_output_kind not in {
            "null",
            "boolean",
            "integer",
            "string",
            "bytes",
            "list",
            "mapping",
        }:
            raise ProofValidationError("candidate raw kind is unsupported")
        if candidate.created_at_policy not in {"clock", "explicit"}:
            raise ProofValidationError("candidate timestamp policy is unsupported")
        _int(candidate.created_at_ns, "created_at_ns")
        try:
            body = json.loads(candidate.receipt_json)
        except (TypeError, json.JSONDecodeError):
            raise ProofValidationError("candidate receipt JSON is invalid") from None
        if _canonical_json(body) != candidate.receipt_json:
            raise ProofValidationError("candidate receipt JSON is not canonical")
        if type(body) is not dict or frozenset(body) != _CANDIDATE_RECEIPT_KEYS:
            raise ProofValidationError("candidate receipt shape differs")
        if _digest(candidate.receipt_json, CANDIDATE_DIGEST_DOMAIN) != candidate.receipt_digest:
            raise ProofValidationError("candidate receipt digest differs")
        expected = {
            "schema": CANDIDATE_SCHEMA,
            "version": 1,
            "plan_id": candidate.plan_id,
            "candidate_id": candidate.candidate_id,
            "slice_id": candidate.slice_id,
            "attempt_id": candidate.attempt_id,
            "commit_oid": candidate.commit_oid,
            "tree_oid": candidate.tree_oid,
            "base_oid": candidate.base_oid,
            "approval_digest": candidate.approval_digest,
            "contract_digest": candidate.contract_digest,
            "source_snapshot_digest": candidate.source_snapshot_digest,
            "created_at_policy": candidate.created_at_policy,
            "created_at_ns": candidate.created_at_ns,
        }
        if any(body.get(name) != value for name, value in expected.items()):
            raise ProofValidationError("candidate receipt binding differs")
        output = body.get("output")
        if type(output) is not dict:
            raise ProofValidationError("candidate redacted output is invalid")
        try:
            validate_redacted_projection(_canonical_json(output))
        except RedactionError:
            raise ProofValidationError(
                "candidate redacted output is invalid"
            ) from None
        if (
            output.get("schema") != REDACTED_OUTPUT_SCHEMA
            or output.get("raw_sha256") != candidate.raw_output_sha256
            or output.get("raw_kind") != candidate.raw_output_kind
            or output.get("raw_framed_sha256")
            != candidate.raw_output_framed_sha256
        ):
            raise ProofValidationError("candidate raw identity differs")

    def complete_verified(
        self,
        plan_id: str,
        verification: AuthorityVerification,
        *,
        verifier: Callable[[AuthorityVerification, Mapping[str, Any]], bool] | None,
        now_ns: int | None = None,
        clock_ns: Callable[[], int] | None = None,
    ) -> bool:
        if not isinstance(verification, AuthorityVerification):
            raise ProofValidationError("authority verification DTO is required")
        if verifier is None or not callable(verifier):
            raise ProofValidationError("fresh authority receipt verifier is required")
        # Treat even a frozen DTO as untrusted input.  Re-running construction
        # invariants here prevents a forged/mutated digest reaching the
        # external verifier or the relational witness transaction.
        verification.__post_init__()
        if now_ns is not None and clock_ns is not None:
            raise ProofValidationError("supply either now_ns or clock_ns")
        if clock_ns is None:
            if now_ns is None:
                clock_ns = time.time_ns
            else:
                fixed_now = _int(now_ns, "now_ns")
                clock_ns = lambda: fixed_now
        if not callable(clock_ns):
            raise ProofValidationError("clock_ns must be callable")
        snapshot = self.store.get_plan(plan_id)
        if snapshot is None:
            raise ProofValidationError("plan does not exist")
        if snapshot.get("state") == "completed_verified":
            if (
                snapshot.get("verification_receipt_digest")
                == verification.receipt_digest
                and snapshot.get("verification_receipt_json")
                == verification.receipt_json
            ):
                self.verify_chain(plan_id)
                return True
            raise ProofValidationError("plan is completed with a different receipt")
        initial_now_ns = _int(clock_ns(), "clock_ns")
        if not (
            verification.issued_at_ns
            <= initial_now_ns
            <= verification.expires_at_ns
        ):
            raise ProofValidationError("authority receipt is not fresh")
        self.verify_chain(plan_id)
        self._validate_terminal_snapshot(snapshot, verification)
        _require_exact_review_pass(
            self.store._connection(),
            plan_row=snapshot,
        )
        try:
            accepted = verifier(verification, dict(snapshot))
        except Exception as exc:
            raise ProofValidationError("authority receipt verifier failed") from exc
        if accepted is not True:
            raise ProofValidationError("authority receipt verifier rejected the receipt")
        _validate_authority_receipt_contract(snapshot, verification)
        bound = self._terminal_tuple(snapshot)

        def complete(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT * FROM bestplan_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if row is None:
                raise ProofValidationError("plan disappeared before terminal CAS")
            current = dict(row)
            if current.get("state") == "completed_verified":
                if (
                    current.get("verification_receipt_digest")
                    == verification.receipt_digest
                    and current.get("verification_receipt_json")
                    == verification.receipt_json
                ):
                    self._verify_chain_snapshot(conn, plan_id)
                    return True
                raise ProofValidationError("plan completed with a different receipt")
            if self._terminal_tuple(current) != bound:
                raise ProofValidationError("terminal projection changed before CAS")
            self._verify_chain_snapshot(conn, plan_id)
            self._validate_terminal_snapshot(current, verification, conn=conn)
            _require_exact_review_pass(conn, plan_row=current)
            from agent.bestplan_state import _validate_stored_plan_row

            try:
                validated = _validate_stored_plan_row(row)
            except Exception as exc:
                raise ProofValidationError("terminal plan revalidation failed") from exc
            if (
                validated.execution_protocol != 2
                or validated.contract is None
                or validated.contract.get("promotion_mode") != "auto_live"
            ):
                raise ProofValidationError("terminal plan is not enrolled auto_live")
            _validate_authority_receipt_contract(current, verification)
            cas_now_ns = _int(clock_ns(), "clock_ns")
            if not (
                verification.issued_at_ns
                <= cas_now_ns
                <= verification.expires_at_ns
            ):
                raise ProofValidationError("authority receipt expired before CAS")
            conn.execute(
                """INSERT INTO bestplan_verification_receipts (
                       plan_id,authority_epoch,event_seq,event_hash,
                       approval_digest,contract_digest,source_snapshot_digest,
                       base_oid,integration_oid,artifact_digest,
                       candidate_set_digest,receipt_json,receipt_digest,
                       verified_at_ns
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    plan_id,
                    verification.authority_epoch,
                    verification.event_seq,
                    verification.event_hash,
                    verification.approval_digest,
                    verification.contract_digest,
                    verification.source_snapshot_digest,
                    current["baseline_revision"],
                    verification.integration_oid,
                    verification.artifact_digest,
                    verification.candidate_set_digest,
                    verification.receipt_json,
                    verification.receipt_digest,
                    cas_now_ns,
                ),
            )
            changed = conn.execute(
                """UPDATE bestplan_plans SET
                       state='completed_verified',
                       completed_at=COALESCE(completed_at, ?),
                       verification_receipt_json=?,
                       verification_receipt_digest=?,
                       verified_at=?
                   WHERE plan_id=? AND state='completed_unverified'
                     AND execution_protocol=2
                     AND current_phase='live_verified'
                     AND proof_authority_epoch=?
                     AND proof_event_seq=?
                     AND proof_event_hash=?""",
                (
                    cas_now_ns / 1_000_000_000.0,
                    verification.receipt_json,
                    verification.receipt_digest,
                    cas_now_ns / 1_000_000_000.0,
                    plan_id,
                    verification.authority_epoch,
                    verification.event_seq,
                    verification.event_hash,
                ),
            ).rowcount
            if changed != 1:
                raise ProofValidationError("terminal compare-and-swap did not match")
            completed_row = conn.execute(
                "SELECT * FROM bestplan_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if completed_row is None:
                raise ProofValidationError("completed plan disappeared after CAS")
            self._validate_completed_snapshot(
                dict(completed_row),
                verification,
                conn=conn,
            )
            return True

        try:
            return bool(self.store._execute_write(complete))
        except sqlite3.IntegrityError as exc:
            raise ProofValidationError("terminal receipt violated a relational invariant") from exc

    @staticmethod
    def _verification_from_completed_row(
        row: Mapping[str, Any],
    ) -> AuthorityVerification:
        receipt_json = row.get("verification_receipt_json")
        receipt_digest = row.get("verification_receipt_digest")
        if not isinstance(receipt_json, str):
            raise ProofValidationError("completed plan has no receipt JSON")
        try:
            body = json.loads(receipt_json)
        except (TypeError, json.JSONDecodeError):
            raise ProofValidationError("completed plan receipt JSON is invalid") from None
        if not isinstance(body, dict):
            raise ProofValidationError("completed plan receipt body is invalid")
        return AuthorityVerification(
            receipt_json=receipt_json,
            receipt_digest=_sha256(
                receipt_digest,
                "verification_receipt_digest",
            ),
            plan_id=_nonempty(body.get("plan_id"), "receipt plan_id"),
            authority_epoch=_nonempty(
                body.get("authority_epoch"),
                "receipt authority_epoch",
            ),
            event_seq=_int(body.get("event_seq"), "receipt event_seq", minimum=1),
            event_hash=_sha256(body.get("event_hash"), "receipt event_hash"),
            approval_digest=_sha256(
                body.get("approval_digest"),
                "receipt approval_digest",
            ),
            contract_digest=_sha256(
                body.get("promotion_contract_digest"),
                "receipt contract_digest",
            ),
            source_snapshot_digest=_sha256(
                body.get("source_snapshot_digest"),
                "receipt source_snapshot_digest",
            ),
            integration_oid=_oid(
                body.get("integration_oid"),
                "receipt integration_oid",
            ),
            artifact_digest=_sha256(
                body.get("artifact_digest"),
                "receipt artifact_digest",
            ),
            candidate_set_digest=_sha256(
                body.get("candidate_set_digest"),
                "receipt candidate_set_digest",
            ),
            issued_at_ns=_int(body.get("issued_at_ns"), "receipt issued_at_ns"),
            expires_at_ns=_int(
                body.get("expires_at_ns"),
                "receipt expires_at_ns",
            ),
        )

    def _validate_completed_snapshot(
        self,
        row: Mapping[str, Any],
        verification: AuthorityVerification,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        # Re-run DTO invariants because a frozen dataclass is still not a trust
        # boundary against deliberately forged Python objects.
        verification.__post_init__()
        if (
            row.get("verification_receipt_json") != verification.receipt_json
            or row.get("verification_receipt_digest")
            != verification.receipt_digest
        ):
            raise ProofValidationError("completed plan receipt differs")
        expected = {
            "plan_id": verification.plan_id,
            "execution_protocol": 2,
            "state": "completed_verified",
            "current_phase": "live_verified",
            "approval_digest": verification.approval_digest,
            "promotion_contract_digest": verification.contract_digest,
            "source_snapshot_digest": verification.source_snapshot_digest,
            "integration_oid": verification.integration_oid,
            "artifact_digest": verification.artifact_digest,
            "candidate_set_digest": verification.candidate_set_digest,
            "proof_authority_epoch": verification.authority_epoch,
            "proof_event_seq": verification.event_seq,
            "proof_event_hash": verification.event_hash,
        }
        if any(row.get(name) != value for name, value in expected.items()):
            raise ProofValidationError("completed plan event pointer/bindings differ")
        timestamp_names = (
            "tests_verified_at",
            "review_verified_at",
            "remote_verified_at",
            "live_verified_at",
            "completed_at",
            "verified_at",
        )
        timestamps: list[float] = []
        for name in timestamp_names:
            value = row.get(name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ProofValidationError("completed plan timestamps are invalid")
            timestamps.append(float(value))
        if timestamps != sorted(timestamps):
            raise ProofValidationError("completed plan timestamps are out of order")
        if timestamps[3] != timestamps[4]:
            raise ProofValidationError("completed/live timestamps differ")
        if not (
            verification.issued_at_ns / 1_000_000_000.0
            <= timestamps[5]
            <= verification.expires_at_ns / 1_000_000_000.0
        ):
            raise ProofValidationError("completed verification timestamp is not fresh")
        from agent.bestplan_state import _validate_stored_plan_row

        try:
            validated = _validate_stored_plan_row(row)
        except Exception as exc:
            raise ProofValidationError("completed plan revalidation failed") from exc
        if (
            validated.execution_protocol != 2
            or validated.contract is None
            or validated.contract.get("promotion_mode") != "auto_live"
        ):
            raise ProofValidationError("completed plan is not enrolled auto_live")
        _validate_authority_receipt_contract(row, verification)
        active_conn = conn or self.store._connection()
        witness = active_conn.execute(
            "SELECT * FROM bestplan_verification_receipts WHERE plan_id=?",
            (verification.plan_id,),
        ).fetchone()
        if witness is None:
            raise ProofValidationError("completed plan has no verification witness")
        witness_values = dict(witness)
        witness_expected = {
            "plan_id": verification.plan_id,
            "authority_epoch": verification.authority_epoch,
            "event_seq": verification.event_seq,
            "event_hash": verification.event_hash,
            "approval_digest": verification.approval_digest,
            "contract_digest": verification.contract_digest,
            "source_snapshot_digest": verification.source_snapshot_digest,
            "base_oid": row.get("baseline_revision"),
            "integration_oid": verification.integration_oid,
            "artifact_digest": verification.artifact_digest,
            "candidate_set_digest": verification.candidate_set_digest,
            "receipt_json": verification.receipt_json,
            "receipt_digest": verification.receipt_digest,
        }
        if any(
            witness_values.get(name) != value
            for name, value in witness_expected.items()
        ):
            raise ProofValidationError("completed verification witness differs")
        verified_at_ns = witness_values.get("verified_at_ns")
        if (
            isinstance(verified_at_ns, bool)
            or not isinstance(verified_at_ns, int)
            or verified_at_ns < 0
            or timestamps[5] != verified_at_ns / 1_000_000_000.0
        ):
            raise ProofValidationError("completed verification witness time differs")
        projected = dict(row)
        projected["state"] = "completed_unverified"
        projected["verified_at"] = None
        self._validate_terminal_snapshot(projected, verification, conn=conn)

    @staticmethod
    def _terminal_tuple(row: Mapping[str, Any]) -> tuple[Any, ...]:
        return tuple(
            row.get(name)
            for name in (
                "plan_id",
                "execution_protocol",
                "state",
                "current_phase",
                "approval_digest",
                "promotion_contract_digest",
                "source_snapshot_digest",
                "baseline_revision",
                "integration_oid",
                "artifact_digest",
                "candidate_set_digest",
                "proof_authority_epoch",
                "proof_event_seq",
                "proof_event_hash",
                "live_verified_at",
            )
        )

    def _validate_terminal_snapshot(
        self,
        row: Mapping[str, Any],
        verification: AuthorityVerification,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        if row.get("plan_id") != verification.plan_id:
            raise ProofValidationError("authority receipt plan binding differs")
        expected = {
            "execution_protocol": 2,
            "state": "completed_unverified",
            "current_phase": "live_verified",
            "approval_digest": verification.approval_digest,
            "promotion_contract_digest": verification.contract_digest,
            "source_snapshot_digest": verification.source_snapshot_digest,
            "integration_oid": verification.integration_oid,
            "artifact_digest": verification.artifact_digest,
            "candidate_set_digest": verification.candidate_set_digest,
            "proof_authority_epoch": verification.authority_epoch,
            "proof_event_seq": verification.event_seq,
            "proof_event_hash": verification.event_hash,
        }
        if any(row.get(name) != value for name, value in expected.items()):
            raise ProofValidationError("authority receipt event pointer/bindings differ")
        if row.get("live_verified_at") is None or row.get("verified_at") is not None:
            raise ProofValidationError("live/terminal verification timestamps differ")
        active_conn = conn or self.store._connection()
        event = active_conn.execute(
            """SELECT * FROM bestplan_proof_events
               WHERE plan_id=? AND stream='authority' AND authority_epoch=?
                 AND event_seq=? AND event_hash=?""",
            (
                verification.plan_id,
                verification.authority_epoch,
                verification.event_seq,
                verification.event_hash,
            ),
        ).fetchone()
        if event is None:
            raise ProofValidationError("authority receipt event pointer is absent")
        event_receipt = ProofEventReceipt.from_row(event)
        self._validate_event_receipt(event_receipt)
        event_values = event_receipt.to_dict()
        if (
            event_values.get("origin") != "authority"
            or event_values.get("kind") != "live_verified"
            or event_values.get("phase") != "live_verified"
            or event_values.get("candidate_set_digest")
            != verification.candidate_set_digest
        ):
            raise ProofValidationError("live event is not authority-origin")
        candidates = active_conn.execute(
            """SELECT * FROM bestplan_candidates
               WHERE plan_id=?
                 AND base_oid=?
                 AND approval_digest=?
                 AND contract_digest=?
                 AND source_snapshot_digest=?
               ORDER BY candidate_id, attempt_id, receipt_digest""",
            (
                verification.plan_id,
                row.get("baseline_revision"),
                verification.approval_digest,
                verification.contract_digest,
                verification.source_snapshot_digest,
            ),
        ).fetchall()
        if not candidates:
            raise ProofValidationError("terminal completion requires a candidate receipt")
        for candidate in candidates:
            self._validate_candidate_receipt(CandidateReceipt.from_row(candidate))
        computed = _candidate_set_digest(
            active_conn,
            verification.plan_id,
            {
                "base_oid": row.get("baseline_revision"),
                "approval_digest": verification.approval_digest,
                "contract_digest": verification.contract_digest,
                "source_snapshot_digest": verification.source_snapshot_digest,
            },
        )
        if computed != verification.candidate_set_digest:
            raise ProofValidationError("terminal candidate receipt aggregate differs")


def make_authority_verification(
    *,
    plan_row: Mapping[str, Any],
    event: ProofEventReceipt,
    observed_local_oid: str,
    observed_remote_oid: str,
    observed_live_release: str,
    observed_live_artifact_digest: str,
    issued_at_ns: int,
    expires_at_ns: int,
) -> AuthorityVerification:
    """Build the canonical DTO an injected verifier must authenticate.

    This helper is deterministic assembly only.  It does not sign, authenticate,
    or confer authority on the resulting DTO.
    """

    if event.stream != "authority" or event.kind != "live_verified":
        raise ProofValidationError("authority receipt requires a live authority event")
    try:
        contract = validate_execution_contract(
            json.loads(str(plan_row["promotion_contract_json"]))
        )
    except Exception as exc:
        raise ProofValidationError("promotion contract is invalid") from exc
    if contract.get("promotion_mode") != "auto_live":
        raise ProofValidationError("authority receipt requires auto_live enrollment")
    observed_local_oid = _oid(observed_local_oid, "observed_local_oid")
    observed_remote_oid = _oid(observed_remote_oid, "observed_remote_oid")
    observed_live_release = _oid(observed_live_release, "observed_live_release")
    observed_live_artifact_digest = _sha256(
        observed_live_artifact_digest, "observed_live_artifact_digest"
    )
    issued_at_ns = _int(issued_at_ns, "issued_at_ns")
    expires_at_ns = _int(expires_at_ns, "expires_at_ns")
    if expires_at_ns <= issued_at_ns:
        raise ProofValidationError("authority receipt expiry must follow issue time")
    if (
        event.plan_id != plan_row.get("plan_id")
        or event.approval_digest != plan_row.get("approval_digest")
        or event.contract_digest != plan_row.get("promotion_contract_digest")
        or event.source_snapshot_digest != plan_row.get("source_snapshot_digest")
        or event.integration_oid != plan_row.get("integration_oid")
        or event.artifact_digest != plan_row.get("artifact_digest")
        or event.candidate_set_digest != plan_row.get("candidate_set_digest")
    ):
        raise ProofValidationError("live event differs from plan projection")
    if (
        observed_local_oid != event.integration_oid
        or observed_remote_oid != event.integration_oid
        or observed_live_release != event.integration_oid
        or observed_live_artifact_digest != event.artifact_digest
    ):
        raise ProofValidationError("observed local/remote/live identity differs")
    candidate_set_digest = _sha256(
        event.candidate_set_digest, "candidate_set_digest"
    )
    body = _authority_receipt_body(
        contract=contract,
        plan_id=event.plan_id,
        authority_epoch=event.authority_epoch,
        event_seq=event.event_seq,
        event_hash=event.event_hash,
        approval_digest=event.approval_digest,
        contract_digest=event.contract_digest,
        source_snapshot_digest=event.source_snapshot_digest,
        base_oid=event.base_oid,
        integration_oid=str(observed_local_oid),
        artifact_digest=str(observed_live_artifact_digest),
        candidate_set_digest=str(candidate_set_digest),
        issued_at_ns=issued_at_ns,
        expires_at_ns=expires_at_ns,
    )
    receipt_json = _canonical_json(body)
    receipt_digest = _digest(receipt_json, AUTHORITY_RECEIPT_DOMAIN)
    return AuthorityVerification(
        receipt_json=receipt_json,
        receipt_digest=receipt_digest,
        plan_id=event.plan_id,
        authority_epoch=event.authority_epoch,
        event_seq=event.event_seq,
        event_hash=event.event_hash,
        approval_digest=event.approval_digest,
        contract_digest=event.contract_digest,
        source_snapshot_digest=event.source_snapshot_digest,
        integration_oid=str(event.integration_oid),
        artifact_digest=str(event.artifact_digest),
        candidate_set_digest=str(candidate_set_digest),
        issued_at_ns=issued_at_ns,
        expires_at_ns=expires_at_ns,
    )


__all__ = [
    "AUTHORITY_PHASE_EDGES",
    "AuthorityVerification",
    "CandidateReceipt",
    "ProofError",
    "ProofEventReceipt",
    "ProofHeadMismatch",
    "ProofLedger",
    "ProofMigrationError",
    "ProofOperationConflict",
    "ProofValidationError",
    "canonical_raw_bytes",
    "install_proof_schema",
    "make_authority_verification",
]
