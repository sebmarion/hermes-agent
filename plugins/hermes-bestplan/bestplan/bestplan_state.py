"""Validated /bestplan envelopes and fail-closed bare-``go`` host ingress."""

from __future__ import annotations

import hashlib
import json
import fcntl
import logging
import math
import os
import re
import sqlite3
import stat
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

from .bestplan_authority_client import BestplanAuthorityClient
from .bestplan_contract import (
    ContractValidationError,
    approval_digest as _approval_digest,
    build_execution_contract,
    contract_digest,
    contract_json,
    render_execution_contract,
    resolve_matching_enrollment,
    source_snapshot_digest,
    source_snapshot_from_json,
    source_snapshot_json,
    source_snapshot_to_dict,
    validate_execution_contract,
)
from .bestplan_local_push import (
    LOCAL_PUSH_ACTIVE_STATES,
    LOCAL_PUSH_MAX_TTL_SECONDS,
    LOCAL_PUSH_REF,
    LOCAL_PUSH_STATES,
    LocalPushStateError,
    build_local_push_record,
    canonical_local_push_json,
    decode_local_push_row,
)
from .bestplan_source import (
    DEFAULT_SOURCE_OPERATION_SECONDS,
    SourceBoundaryError,
    capture_legacy_v1_fingerprint,
    capture_source_snapshot,
    inspect_legacy_v1_workspace,
    inspect_workspace_boundary,
    resolve_repo_identity,
    strong_source_capture_supported,
)
from .execution_plan import ExecutionPlan, PlanValidationError, compile_execution_plan

logger = logging.getLogger(__name__)

BESTPLAN_ENVELOPE_START = "<<<HERMES_BESTPLAN_V1>>>"
BESTPLAN_ENVELOPE_END = "<<<END_HERMES_BESTPLAN_V1>>>"
BESTPLAN_HOST_CAPABILITY_VERSION = 2
_ENVELOPE_RE = re.compile(
    re.escape(BESTPLAN_ENVELOPE_START)
    + r"\s*(?P<payload>\{.*?\})\s*"
    + re.escape(BESTPLAN_ENVELOPE_END),
    re.DOTALL,
)
_ENVELOPE_BLOCK_RE = re.compile(
    re.escape(BESTPLAN_ENVELOPE_START)
    + r".*?"
    + re.escape(BESTPLAN_ENVELOPE_END),
    re.DOTALL,
)


class PlanState:
    PROVISIONAL = "provisional"
    PENDING = "pending"
    APPROVED = "approved"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED_UNVERIFIED = "completed_unverified"
    COMPLETED_VERIFIED = "completed_verified"
    COMPLETED_LOCAL = "completed_local"
    REJECTED = "rejected"
    FAILED = "failed"


_OPEN_STATES = (
    PlanState.PENDING,
    PlanState.APPROVED,
    PlanState.RUNNING,
    PlanState.WAITING,
)
_MAX_V1_SLICES = 2
_LANE_FOR_SLICE = {
    ("implement", "fast_fallback"): "code_worker",
    ("review", "frontier_review"): "smart_reviewer",
}

class BestplanError(ValueError):
    """Raised when an envelope, state transition, or route is unsafe."""


class BaselineFingerprintError(BestplanError):
    """Raised when the host cannot compute a strong workspace baseline."""


@dataclass(frozen=True)
class PlanCapture:
    executable: bool
    response: str
    plan_id: Optional[str] = None
    digest: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class ResolvedGo:
    # ``resolved`` means the host consumed this turn.  It is intentionally true
    # for fail-closed outcomes as well as successful dispatch.
    resolved: bool
    status: str
    plan_id: Optional[str] = None
    delegation_id: Optional[str] = None
    reason: Optional[str] = None
    error: Optional[str] = None

    @property
    def response(self) -> str:
        if self.status == "push_declined":
            return (
                f"Local `main` remains at the checked commit for plan {self.plan_id}. "
                "Hermes did not change the remote."
            )
        if self.status == "push_complete":
            return (
                f"Pushed the exact checked local `main` commit for plan "
                f"{self.plan_id}; remote `main` now matches it."
            )
        if self.status == "push_in_flight":
            return (
                f"The exact push for plan {self.plan_id} is already in flight. "
                "No second push was started."
            )
        if self.status == "push_effect_unknown":
            return (
                f"The exact push result for plan {self.plan_id} is not yet proven. "
                "Reply `push` again to reconcile the same target; Hermes will not "
                "force-push."
            )
        if self.status == "push_expired":
            return (
                f"The push confirmation for plan {self.plan_id} expired. "
                "Hermes did not start a new remote write."
            )
        if self.status in {"push_stale", "push_context_mismatch", "push_ambiguous"}:
            return (
                f"The push confirmation for plan {self.plan_id or '(unknown)'} "
                f"failed closed ({self.status}): "
                f"{self.reason or self.error or 'the exact target is not provable'}."
            )
        if self.status == "waiting":
            return (
                f"Plan {self.plan_id} was dispatched as delegation "
                f"{self.delegation_id or '(pending id)'}. Status: waiting for "
                "independent completion evidence."
            )
        if self.status in {"possibly_dispatched", "dispatch_in_flight"}:
            return (
                f"Plan {self.plan_id} may already be active as delegation "
                f"{self.delegation_id or '(identity persisted)'}. The dispatch outcome "
                "is unknown/possibly dispatched; retry is idempotent and will reconcile it."
            )
        return f"Plan execution was not started ({self.status}): {self.reason or self.error or 'fail-closed'}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolved": self.resolved,
            "status": self.status,
            "plan_id": self.plan_id,
            "delegation_id": self.delegation_id,
            "reason": self.reason,
            "error": self.error,
            "response": self.response,
        }

    def to_agent_result(
        self,
        *,
        conversation_history: list[dict[str, Any]],
        user_message: str,
        host_agent: Any = None,
    ) -> dict[str, Any]:
        """Build a terminal host result without entering the model loop."""
        messages = [dict(item) for item in conversation_history]
        messages.append({"role": "user", "content": user_message})
        messages.append({"role": "assistant", "content": self.response})
        if host_agent is not None:
            from agent.agent_runtime_helpers import repair_message_sequence

            repair_message_sequence(host_agent, messages)
            persist = getattr(host_agent, "_persist_session", None)
            if callable(persist):
                persist(messages, conversation_history)
        return {
            "final_response": self.response,
            "messages": messages,
            "api_calls": 0,
            "completed": True,
            "host_ingress": self.to_dict(),
        }


def _active_profile() -> str:
    return str(os.environ.get("HERMES_PROFILE") or "").strip()


def _canonical_workspace(workspace: str) -> str:
    return str(Path(workspace or os.getcwd()).expanduser().resolve())


def _workspace_hint(workspace: str) -> str:
    """Preserve the hint; trusted helper resolves filesystem identity and relativity."""

    return str(workspace or ".")


_RUNTIME_SECRET_CONTAINERS = {"auth", "cookies", "extra_headers", "headers"}
_RUNTIME_SECRET_PARTS = {
    "api_key", "authorization", "bearer", "cookie", "credential",
    "key", "password", "secret", "token", "tokens",
}
_RUNTIME_NONSECRET_TOKEN_KEYS = {"max_output_tokens", "max_tokens"}
_RUNTIME_URL_KEYS = {"api_base", "base_url", "endpoint", "endpoint_url", "url"}
_RUNTIME_SECRET_VALUE_RE = re.compile(
    r"(?i)(?:\b(?:bearer|basic)\s+\S+|"
    r"(?:api[-_]?key|authorization|password|secret|token)\s*[=:]\s*[^\s&]+)"
)
_V2_RUNTIME_IDENTITY_STRING_FIELDS = frozenset(
    {
        "api_mode",
        "model",
        "provider",
        "route",
        "runtime_fingerprint",
        "sandbox_backend",
        "sandbox_policy_digest",
        "candidate_host_runtime_digest",
    }
)
_V2_RUNTIME_IDENTITY_LIST_FIELDS = frozenset(
    {"bestplan_toolsets", "toolsets"}
)
_V2_RUNTIME_IDENTITY_INT_FIELDS = {
    "candidate_policy_version": 1_000_000,
    "candidate_request_budget": 10_000,
    "candidate_token_budget": 100_000_000,
    "candidate_max_iterations": 500,
    "candidate_max_output_tokens": 32_768,
}
_V2_RUNTIME_IDENTITY_NUMBER_FIELDS = {
    "candidate_timeout_seconds": 86_400.0,
    "candidate_capability_ttl_seconds": 86_400.0,
}
_V2_RUNTIME_EXECUTION_FIELDS = frozenset(
    {
        "api_key",
        "api_mode",
        "args",
        "base_url",
        "bestplan_toolsets",
        "command",
        "max_output_tokens",
        "model",
        "provider",
        "request_overrides",
        "route",
        "runtime_fingerprint",
        "sandbox_backend",
        "sandbox_policy_digest",
        "toolsets",
    }
)
_V2_RUNTIME_MAX_ITEMS = 64
_V2_RUNTIME_MAX_STRING_BYTES = 1024
_V2_RUNTIME_MAX_JSON_BYTES = 32768


def _normalized_runtime_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")


def _runtime_key_is_sensitive(key: Any) -> bool:
    normalized = _normalized_runtime_key(key)
    if normalized in _RUNTIME_NONSECRET_TOKEN_KEYS:
        return False
    if normalized in _RUNTIME_SECRET_CONTAINERS:
        return True
    parts = set(normalized.split("_"))
    return bool(parts & _RUNTIME_SECRET_PARTS) or normalized.endswith("api_key")


def _sanitize_runtime_string(value: str, *, key: Any = "") -> str:
    normalized = _normalized_runtime_key(key)
    if normalized in _RUNTIME_URL_KEYS:
        try:
            schemeless = "://" not in value and not value.startswith(("/", "./", "../"))
            parsed = urlsplit(f"//{value}" if schemeless else value)
            if parsed.hostname:
                host = parsed.hostname.casefold()
                if ":" in host and not host.startswith("["):
                    host = f"[{host}]"
                netloc = host
                if parsed.port is not None:
                    netloc += f":{parsed.port}"
                if schemeless:
                    return f"{netloc}{parsed.path or ''}"
                return urlunsplit(
                    (parsed.scheme.casefold(), netloc, parsed.path or "", "", "")
                )
        except (TypeError, ValueError):
            return "<redacted-url>"
        return value.split("?", 1)[0].split("#", 1)[0]
    if _RUNTIME_SECRET_VALUE_RE.search(value):
        return "<redacted>"
    return value


def _bounded_v2_runtime_string(value: Any) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise BestplanError("protocol-2 runtime identity has an invalid field type")
    if value == "":
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise BestplanError("protocol-2 runtime identity has invalid text") from None
    if (
        len(encoded) > _V2_RUNTIME_MAX_STRING_BYTES
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise BestplanError("protocol-2 runtime identity text is out of bounds")
    return value


def _sanitize_v2_runtime_identity(value: Any) -> list[dict[str, Any]]:
    """Project untrusted resolver output onto the retry identity contract."""

    if type(value) is not list or len(value) > _V2_RUNTIME_MAX_ITEMS:
        raise BestplanError("protocol-2 runtime identity must be a bounded list")
    projected: list[dict[str, Any]] = []
    for item in value:
        if type(item) is not dict:
            raise BestplanError("protocol-2 runtime identity item must be a mapping")
        if any(type(key) is not str for key in item):
            raise BestplanError("protocol-2 runtime identity keys must be strings")
        safe: dict[str, Any] = {}
        for key in sorted(_V2_RUNTIME_IDENTITY_STRING_FIELDS):
            if key not in item:
                continue
            normalized = _bounded_v2_runtime_string(item[key])
            if normalized is not None:
                safe[key] = normalized
        for key in _V2_RUNTIME_IDENTITY_LIST_FIELDS:
            if key not in item or item[key] is None:
                continue
            raw_items = item[key]
            if type(raw_items) is not list or len(raw_items) > _V2_RUNTIME_MAX_ITEMS:
                raise BestplanError(
                    "protocol-2 runtime identity list field is invalid"
                )
            normalized_items = []
            for raw_item in raw_items:
                normalized = _bounded_v2_runtime_string(raw_item)
                if normalized is None:
                    raise BestplanError(
                        "protocol-2 runtime identity list item is invalid"
                    )
                normalized_items.append(normalized)
            safe[key] = sorted(dict.fromkeys(normalized_items))
        for key, maximum in _V2_RUNTIME_IDENTITY_INT_FIELDS.items():
            if key not in item:
                continue
            raw_number = item[key]
            if type(raw_number) is not int or not 1 <= raw_number <= maximum:
                raise BestplanError(
                    "protocol-2 runtime candidate policy is invalid"
                )
            safe[key] = raw_number
        for key, maximum in _V2_RUNTIME_IDENTITY_NUMBER_FIELDS.items():
            if key not in item:
                continue
            raw_number = item[key]
            if (
                isinstance(raw_number, bool)
                or not isinstance(raw_number, (int, float))
                or not math.isfinite(float(raw_number))
                or not 0 < float(raw_number) <= maximum
            ):
                raise BestplanError(
                    "protocol-2 runtime candidate policy is invalid"
                )
            safe[key] = float(raw_number)
        projected.append(safe)
    encoded = json.dumps(
        projected,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded.encode("ascii")) > _V2_RUNTIME_MAX_JSON_BYTES:
        raise BestplanError("protocol-2 runtime identity projection is oversized")
    return projected


def _filter_v2_runtime_execution(value: Any) -> list[dict[str, Any]]:
    """Drop unsupported resolver fields before crossing the dispatch boundary."""

    _sanitize_v2_runtime_identity(value)
    return [
        {
            key: item[key]
            for key in sorted(_V2_RUNTIME_EXECUTION_FIELDS)
            if key in item
        }
        for item in value
    ]


def _bind_v2_candidate_toolsets(
    value: Any, tasks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Replace resolver-selected tools with the approved process-free set."""

    if type(value) is not list or len(value) != len(tasks):
        raise BestplanError("protocol-2 runtime count is invalid")
    bound: list[dict[str, Any]] = []
    for runtime, task in zip(value, tasks):
        if type(runtime) is not dict or not isinstance(task, dict):
            raise BestplanError("protocol-2 runtime item is invalid")
        item = dict(runtime)
        item.pop("runtime_identity", None)
        candidate_toolsets = [
            "read_only_files" if bool(task.get("_bestplan_read_only")) else "file"
        ]
        item["toolsets"] = candidate_toolsets
        item["bestplan_toolsets"] = list(candidate_toolsets)
        bound.append(item)
    return bound


def sanitize_runtime_metadata(
    value: Any,
    *,
    _key: Any = "",
    execution_protocol: int = 1,
) -> Any:
    """Return JSON-safe runtime identity data with credential surfaces removed."""
    if execution_protocol == 2:
        return _sanitize_v2_runtime_identity(value)
    if isinstance(value, dict):
        return {
            str(key): sanitize_runtime_metadata(item, _key=key)
            for key, item in sorted(value.items())
            if not _runtime_key_is_sensitive(key)
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_runtime_metadata(item, _key=_key) for item in value]
    if isinstance(value, str):
        return _sanitize_runtime_string(value, key=_key)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def _strip_bestplan_envelope(value: Any) -> str:
    visible = _ENVELOPE_BLOCK_RE.sub("", str(value or ""))
    start = visible.find(BESTPLAN_ENVELOPE_START)
    if start >= 0:
        # A missing end sentinel makes the remainder untrusted machine payload.
        visible = visible[:start]
    return visible.replace(BESTPLAN_ENVELOPE_END, "").strip()


def _bestplan_marked_blocks(value: Any) -> list[str]:
    """Return complete host-marked receipt blocks without trusting their data."""

    from .bestplan_orchestrator import (
        RECEIPT_BEGIN,
        RECEIPT_BEGIN_V1,
        RECEIPT_END,
        RECEIPT_END_V1,
    )

    text = str(value or "")
    blocks: list[str] = []
    for begin, end in (
        (RECEIPT_BEGIN, RECEIPT_END),
        (RECEIPT_BEGIN_V1, RECEIPT_END_V1),
    ):
        cursor = 0
        while True:
            start = text.find(begin, cursor)
            if start < 0:
                break
            finish = text.find(end, start + len(begin))
            if finish < 0:
                break
            blocks.append(text[start : finish + len(end)].strip())
            cursor = finish + len(end)
    return blocks


def _bestplan_receipt_metadata(
    host_metadata: Mapping[str, Any] | None,
    response: str,
    body: str,
) -> dict[str, Any] | None:
    """Validate host-owned receipt metadata against the exact plan body.

    The response is model-visible text and is never the source of model
    identities.  If it contains a receipt marker, the marker must match the
    host-owned metadata; otherwise the summary omits model details.
    """

    from .bestplan_orchestrator import _valid_v2_receipt_metadata

    if not isinstance(host_metadata, Mapping):
        return None
    metadata = dict(host_metadata)
    if not _valid_v2_receipt_metadata(metadata, body):
        return None

    blocks = _bestplan_marked_blocks(response)
    if not blocks:
        from .bestplan_orchestrator import RECEIPT_BEGIN, RECEIPT_BEGIN_V1

        if RECEIPT_BEGIN in response or RECEIPT_BEGIN_V1 in response:
            # A receipt marker without one complete matching block is an
            # invalid model-visible artifact, not a source of host metadata.
            return None
        return metadata
    if len(blocks) != 1:
        return None
    try:
        lines = blocks[0].splitlines()
        marker_metadata = json.loads(lines[1])
    except (IndexError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(marker_metadata, dict) or marker_metadata != metadata:
        return None
    return metadata


def _strip_bestplan_internal_artifacts(value: Any) -> str:
    """Remove the machine envelope and host receipt from human-facing text."""

    visible = _strip_bestplan_envelope(value)
    from .bestplan_orchestrator import (
        RECEIPT_BEGIN,
        RECEIPT_BEGIN_V1,
        RECEIPT_END,
        RECEIPT_END_V1,
    )

    for begin, end in (
        (RECEIPT_BEGIN, RECEIPT_END),
        (RECEIPT_BEGIN_V1, RECEIPT_END_V1),
    ):
        while True:
            start = visible.find(begin)
            if start < 0:
                break
            finish = visible.find(end, start + len(begin))
            if finish < 0:
                visible = visible[:start].rstrip()
                break
            visible = visible[:start] + visible[finish + len(end) :]
    return visible.strip()


def _render_host_receipt_warning(code: Any) -> str:
    """Render only a known host warning, never warning text from the model."""

    if code != "receipt_persistence_failed":
        return ""
    return (
        "Warning\n"
        "- The BestPlan receipt audit could not be persisted; the plan remains "
        "pending and must be checked before approval."
    )


def compute_baseline_fingerprint(workspace: str) -> str:
    """Compatibility wrapper for the stable Git source/protected-state proof."""

    workspace_hint = _workspace_hint(workspace)
    if not strong_source_capture_supported():
        try:
            _, fingerprint = capture_legacy_v1_fingerprint(
                workspace_hint,
                time.monotonic() + DEFAULT_SOURCE_OPERATION_SECONDS,
            )
            return fingerprint
        except SourceBoundaryError as exc:
            raise BaselineFingerprintError(
                "candidate-only legacy git baseline unavailable for "
                f"{workspace_hint}: {exc.code}: {exc}"
            ) from exc
    try:
        repo = resolve_repo_identity(workspace_hint)
        snapshot = capture_source_snapshot(
            repo, time.monotonic() + DEFAULT_SOURCE_OPERATION_SECONDS,
        )
        return snapshot.fingerprint
    except SourceBoundaryError as exc:
        raise BaselineFingerprintError(
            f"strong git baseline unavailable for {workspace_hint}: "
            f"{exc.code}: {exc}"
        ) from exc


def _manifest_digest(manifest: dict[str, Any]) -> str:
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_go_trigger(text: str) -> bool:
    return str(text or "").strip().casefold() == "go"


def _extract_envelope(response: str) -> tuple[str, ExecutionPlan, dict[str, Any]]:
    matches = list(_ENVELOPE_RE.finditer(str(response or "")))
    if len(matches) != 1:
        raise BestplanError("response must contain exactly one explicit bestplan envelope")
    raw_envelope = matches[0].group(0).strip()
    try:
        envelope = json.loads(matches[0].group("payload"))
    except json.JSONDecodeError as exc:
        raise BestplanError(f"bestplan envelope is not valid JSON: {exc}") from exc
    if not isinstance(envelope, dict) or set(envelope) != {"version", "manifest"}:
        raise BestplanError("bestplan envelope must contain only version and manifest")
    if envelope.get("version") != 1 or isinstance(envelope.get("version"), bool):
        raise BestplanError("bestplan envelope version must be integer 1")
    try:
        plan = compile_execution_plan(envelope.get("manifest"))
    except PlanValidationError as exc:
        raise BestplanError(str(exc)) from exc
    return raw_envelope, plan, plan.to_manifest()


def _validated_write_lease(workspace: str, lease: str) -> str:
    raw = str(lease or "").strip()
    if not raw:
        raise BestplanError("V1 implementation requires a nonempty write lease")
    candidate = Path(raw)
    if candidate.is_absolute():
        raise BestplanError(f"write lease must be relative to workspace: {raw}")
    if any(part == ".." for part in candidate.parts):
        raise BestplanError(f"write lease traversal is forbidden: {raw}")
    root = Path(workspace).resolve()
    resolved = (root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise BestplanError(f"write lease escapes workspace: {raw}")
    return candidate.as_posix()


def _v1_plan_constraints(plan: ExecutionPlan, *, workspace: Optional[str] = None) -> None:
    if len(plan.slices) > _MAX_V1_SLICES:
        raise BestplanError(f"V1 supports at most {_MAX_V1_SLICES} slices; got {len(plan.slices)}")
    if len(plan.dependency_waves) != 1:
        raise BestplanError("V1 supports only one independent wave; dependencies are not allowed")
    canonical_workspace = _canonical_workspace(workspace) if workspace is not None else None
    kinds = {item.kind for item in plan.slices}
    if kinds == {"review"}:
        if plan.mode != "sota":
            raise BestplanError("V1 review-only manifest requires mode=sota")
        if plan.risk != "high":
            raise BestplanError("V1 review-only manifest requires risk=high")
    elif kinds == {"implement"}:
        if plan.mode != "delegate":
            raise BestplanError("V1 implementation manifest requires mode=delegate")
    else:
        raise BestplanError("V1 cannot mix implementation and review slices")
    for item in plan.slices:
        if item.depends_on:
            raise BestplanError(f"V1 slice {item.id} has dependencies")
        if (item.kind, item.capability) not in _LANE_FOR_SLICE:
            raise BestplanError(
                f"V1 cannot route slice {item.id} (kind={item.kind}, capability={item.capability})"
            )
        if canonical_workspace is not None and _canonical_workspace(item.workspace or "") != canonical_workspace:
            raise BestplanError(
                f"V1 slice {item.id} workspace must equal captured workspace {canonical_workspace}"
            )
        if item.kind == "implement":
            if item.read_only:
                raise BestplanError(f"V1 implementation slice {item.id} requires read_only=false")
            if not item.allowed_paths:
                raise BestplanError(f"V1 implementation slice {item.id} requires a nonempty write lease")
            for lease in item.allowed_paths:
                _validated_write_lease(canonical_workspace or _canonical_workspace(item.workspace), lease)
        else:
            if item.capability != "frontier_review" or not item.read_only:
                raise BestplanError(
                    f"V1 review slice {item.id} requires frontier_review/read_only=true"
                )
            if item.allowed_paths:
                raise BestplanError(
                    f"V1 review slice {item.id} requires allowed_paths=[]"
                )


def _minimum_change_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BestplanError(f"{field} must be a nonempty relative file path")
    text = value.strip()
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or text != path.as_posix()
        or text in {".", ".."}
        or text.endswith("/")
        or ".." in path.parts
    ):
        raise BestplanError(f"{field} must be a normalized relative file path")
    return text


def validate_bestplan_minimum_change(
    plan: ExecutionPlan,
    *,
    workspace: str,
    evidence_paths: tuple[str, ...] | list[str],
) -> None:
    """Apply the deterministic scope ceiling for generated BestPlan plans.

    This is intentionally narrower than the generic execution-plan contract:
    generated implementation plans get one slice, exact file leases, and no
    write path outside the candidate-reported evidence set.  It is a veto-only
    validator; it never expands model-proposed scope or treats a model claim as
    proof that a file was actually inspected.
    """
    if not isinstance(plan, ExecutionPlan):
        raise BestplanError("minimum-change plan is invalid")
    if len(plan.slices) != 1:
        raise BestplanError("minimum-change plans require one implementation slice")
    item = plan.slices[0]
    if item.kind != "implement" or item.read_only:
        raise BestplanError("minimum-change plans require one writable implementation slice")
    if item.depends_on:
        raise BestplanError("minimum-change implementation slice cannot have dependencies")

    evidence = tuple(
        _minimum_change_path(value, "evidence scope") for value in evidence_paths
    )
    if not evidence or len(set(evidence)) != len(evidence):
        raise BestplanError("minimum-change evidence scope must be nonempty and unique")
    evidence_set = set(evidence)

    leases = tuple(
        _minimum_change_path(value, "write lease") for value in item.allowed_paths
    )
    artifacts = tuple(
        _minimum_change_path(value, "expected artifact")
        for value in item.expected_artifacts
    )
    if not leases or len(set(leases)) != len(leases):
        raise BestplanError("minimum-change write leases must be nonempty and unique")
    if not artifacts or len(set(artifacts)) != len(artifacts):
        raise BestplanError("minimum-change expected artifacts must be nonempty and unique")
    if set(leases) != set(artifacts):
        raise BestplanError(
            "minimum-change write leases must exactly cover expected artifacts"
        )
    if not set(leases) <= evidence_set:
        raise BestplanError("write lease falls outside the evidence scope")

    for lease in leases:
        _validated_write_lease(workspace, lease)
    try:
        from .bestplan_local import _local_check_config_from_manifest

        _local_check_config_from_manifest(plan.to_manifest())
    except Exception as exc:
        raise BestplanError(
            "minimum-change implementation requires exact pytest acceptance"
        ) from exc


def _plan_to_delegate_tasks(
    plan: ExecutionPlan, *, workspace: Optional[str] = None,
) -> list[dict[str, Any]]:
    _v1_plan_constraints(plan, workspace=workspace)
    tasks = []
    for index, item in enumerate(plan.slices):
        tasks.append({
            "goal": item.goal,
            "context": "\n".join([
                f"Slice {item.id}: {item.goal}",
                f"Workspace: {item.workspace or '.'}",
                f"Allowed paths: {', '.join(item.allowed_paths)}",
                f"Expected artifacts: {', '.join(item.expected_artifacts)}",
                f"Acceptance: {'; '.join(item.acceptance)}",
            ]),
            "route": _LANE_FOR_SLICE[(item.kind, item.capability)],
            "role": "leaf",
            "_bestplan_slice_id": item.id,
            "_bestplan_manifest_index": index,
            "_bestplan_depends_on": list(item.depends_on),
            "_bestplan_read_only": item.read_only,
            "_bestplan_leases": list(item.allowed_paths),
            "_bestplan_workspace": _canonical_workspace(workspace or item.workspace),
            "_bestplan_expected_artifacts": list(item.expected_artifacts),
            "_bestplan_acceptance": list(item.acceptance),
        })
    return tasks


def _local_review_runtime_tasks(workspace: str) -> list[dict[str, Any]]:
    """Return the two exact read-only lanes required by automatic review."""

    canonical_workspace = _canonical_workspace(workspace)
    return [
        {
            "goal": "Review the exact checked BestPlan integration",
            "context": "Host-owned automatic code review; return strict JSON only.",
            "route": slot,
            "role": "leaf",
            "_bestplan_slice_id": f"automatic-review-{slot}",
            "_bestplan_manifest_index": index,
            "_bestplan_depends_on": [],
            "_bestplan_read_only": True,
            "_bestplan_leases": [],
            "_bestplan_workspace": canonical_workspace,
            "_bestplan_expected_artifacts": [],
            "_bestplan_acceptance": [
                "Return one exact hermes.bestplan.review-verdict.v1 object"
            ],
        }
        for index, slot in enumerate(("smart_reviewer", "code_worker"))
    ]


def _render_authoritative_manifest(
    plan: ExecutionPlan,
    *,
    workspace: str,
    digest: str,
    contract: dict[str, Any] | None = None,
) -> str:
    def escaped(value: Any) -> str:
        encoded = json.dumps(str(value), ensure_ascii=True)
        return encoded[1:-1]

    lines = [
        "Authoritative executable manifest (host-rendered):",
        f"- digest={digest}",
        f"- mode: {escaped(plan.mode)}",
        f"- risk: {escaped(plan.risk)}",
        f"- workspace: {escaped(_canonical_workspace(workspace))}",
    ]
    local_contract = (
        isinstance(contract, Mapping)
        and contract.get("schema") == "hermes.bestplan.local-go.v1"
    )
    for item in plan.slices:
        route = _LANE_FOR_SLICE[(item.kind, item.capability)]
        leases = (
            ", ".join(escaped(value) for value in item.allowed_paths)
            if item.allowed_paths
            else "none (read-only)"
        )
        artifacts = (
            ", ".join(escaped(value) for value in item.expected_artifacts)
            if item.expected_artifacts
            else "none"
        )
        acceptance = "; ".join(escaped(value) for value in item.acceptance)
        slice_lines = [
            f"- slice {escaped(item.id)}:",
            f"  - route: {escaped(route)}",
            f"  - goal: {escaped(item.goal)}",
            f"  - kind/capability: {escaped(item.kind)}/{escaped(item.capability)}",
            f"  - read_only: {str(item.read_only).lower()}",
            f"  - write leases: {leases}",
            f"  - expected artifacts: {artifacts}",
            f"  - acceptance: {acceptance}",
        ]
        if not local_contract:
            from .bestplan_sandbox import sandbox_backend_identity

            sandbox = sandbox_backend_identity(
                workspace=workspace,
                allowed_paths=item.allowed_paths,
                read_only=item.read_only,
            )
            slice_lines[-1:-1] = [
                f"  - sandbox backend: {escaped(sandbox['backend'])}",
                f"  - sandbox policy digest: {escaped(sandbox['policy_digest'])}",
            ]
        lines.extend(slice_lines)
    if local_contract:
        from .bestplan_local import render_local_go_contract

        lines.extend(render_local_go_contract(contract).splitlines())
    else:
        contract_lines = render_execution_contract(
            plan, contract, digest, _canonical_workspace(workspace),
        ).splitlines()
        protocol_index = next(
            index
            for index, line in enumerate(contract_lines)
            if line.startswith("- execution protocol:")
        )
        lines.extend(contract_lines[protocol_index:])
    return "\n".join(lines)


def _model_identity(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    model = value.get("model")
    provider = value.get("provider")
    if not isinstance(model, str) or not model.strip():
        return None
    if not isinstance(provider, str) or not provider.strip():
        return None
    return provider.strip(), model.strip()


def _status_counts(attempts: list[Any]) -> dict[tuple[str, str], dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            continue
        identity = _model_identity(attempt.get("resolved")) or _model_identity(
            attempt.get("configured")
        )
        if identity is None:
            continue
        item = grouped.setdefault(
            identity,
            {"success": 0, "failed": 0, "timeout": 0, "reasons": []},
        )
        status = str(attempt.get("status") or "unknown")
        if status == "success":
            item["success"] += 1
        elif status == "timeout":
            item["timeout"] += 1
        else:
            item["failed"] += 1
        reason = attempt.get("reason_code")
        if isinstance(reason, str) and reason.strip():
            item["reasons"].append(reason.replace("_", " "))
    return grouped


def _display_model_name(value: Any) -> str:
    """Return a short model name suitable for an executive status line."""

    raw = str(value or "").strip()
    if "/" in raw:
        raw = raw.rsplit("/", 1)[-1]
    known = {
        "deepseek-v4-flash-0731": "DeepSeek v4 Flash",
        "gpt-5.6-sol": "GPT-5.6 Sol",
    }
    if raw in known:
        return known[raw]
    words = re.split(r"[-_]+", raw)
    brand_names = {"deepseek": "DeepSeek", "claude": "Claude", "gemini": "Gemini"}
    return " ".join(
        word.upper()
        if word.casefold() in {"gpt", "api", "llm"}
        else brand_names.get(word.casefold(), word.capitalize())
        for word in words
        if word
    ) or "Unknown model"


def _model_outcome_text(counts: Mapping[str, Any]) -> str:
    outcomes: list[str] = []
    success = int(counts.get("success") or 0)
    failed = int(counts.get("failed") or 0)
    timeout = int(counts.get("timeout") or 0)
    if success:
        outcomes.append(f"{success} successful run{'s' if success != 1 else ''}")
    unsuccessful = failed + timeout
    if unsuccessful:
        outcomes.append(
            f"{unsuccessful} unsuccessful run{'s' if unsuccessful != 1 else ''}"
        )
    return "; ".join(outcomes) or "no usable result"


def _render_model_summary(metadata: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(metadata, Mapping):
        return ["- Planning provenance is unavailable."]

    attempts = [
        attempt
        for attempt in list(metadata.get("attempts") or [])
        if isinstance(attempt, Mapping)
    ]
    successes = sum(
        str(attempt.get("status") or "").casefold() == "success"
        for attempt in attempts
    )
    effective_count = metadata.get("effective_count")
    quorum_required = metadata.get("quorum_required")
    run_id = metadata.get("run_id")
    status = str(metadata.get("status") or "unknown").casefold()
    lines: list[str] = []

    if isinstance(effective_count, int) and not isinstance(effective_count, bool):
        required = (
            f"{quorum_required} required"
            if isinstance(quorum_required, int) and not isinstance(quorum_required, bool)
            else "quorum requirement unavailable"
        )
        run_label = f" `{_inline_text(run_id)}`" if run_id else ""
        lines.append(
            f"- Run{run_label}: {successes}/{effective_count} explorer results usable; "
            f"{required}; terminal status: {status}."
        )

    for attempt in attempts:
        identity = _model_identity(attempt.get("resolved")) or _model_identity(
            attempt.get("configured")
        )
        if identity is None:
            continue
        provider, model = identity
        name = _inline_text(attempt.get("explorer") or "explorer")
        attempt_status = str(attempt.get("status") or "unknown").casefold()
        reason = attempt.get("reason_code")
        detail = f"; reason: {_inline_text(reason)}" if reason else ""
        lines.append(
            f"- Explorer `{name}`: `{_inline_text(provider)}/{_inline_text(model)}` "
            f"— {attempt_status}{detail}."
        )

    synthesizer = metadata.get("synthesizer")
    if isinstance(synthesizer, Mapping):
        identity = _model_identity(synthesizer.get("resolved")) or _model_identity(
            synthesizer.get("configured")
        )
        if identity is not None:
            provider, model = identity
            name = _inline_text(synthesizer.get("name") or "synthesizer")
            synth_status = str(synthesizer.get("status") or "unknown").casefold()
            status_text = {
                "success": "wrote the final plan",
                "failed": "could not write the final plan",
                "timeout": "did not finish the final plan",
                "not_started": "was not started",
            }.get(synth_status, "status unavailable")
            reason = synthesizer.get("reason_code")
            detail = f"; reason: {_inline_text(reason)}" if reason else ""
            lines.append(
                f"- Synthesizer `{name}`: `{_inline_text(provider)}/{_inline_text(model)}` "
                f"— {status_text}{detail}."
            )
    return lines or ["- Planning provenance is unavailable."]


def _plan_text(plan: ExecutionPlan) -> str:
    values: list[str] = [plan.merge_policy, plan.stop_condition]
    values.extend(plan.escalation_predicates)
    for item in plan.slices:
        values.append(item.goal)
        values.extend(item.expected_artifacts)
        values.extend(item.acceptance)
    return " ".join(_inline_text(value) for value in values).casefold()


def _is_evidence_hold_plan(plan: ExecutionPlan) -> bool:
    text = _plan_text(plan)
    return (
        "hold" in text
        and ("evidence" in text or "authorized" in text or "current-state" in text)
    )


def _artifact_reference(value: Any) -> str:
    text = _inline_text(value).rstrip(".")
    for separator in (" containing ", " that ", " with ", " — "):
        position = text.casefold().find(separator)
        if position > 0:
            text = text[:position].rstrip(" .")
    return text


def _is_plans_path(value: Any) -> bool:
    path = _artifact_reference(value).replace("\\", "/").strip("`")
    return path == ".plans" or path.startswith(".plans/")


def _is_documentation_only_plan(plan: ExecutionPlan) -> bool:
    writable = [item for item in plan.slices if not item.read_only]
    if not writable:
        return False
    return all(
        _is_plans_path(path)
        for item in writable
        for path in (*item.allowed_paths, *item.expected_artifacts)
    )


def _render_executive_actions(plan: ExecutionPlan, *, hold: bool) -> list[str]:
    lines: list[str] = []
    for item in plan.slices:
        artifacts = tuple(
            dict.fromkeys(
                _artifact_reference(value)
                for value in item.expected_artifacts
                if _artifact_reference(value)
            )
        )
        if hold and artifacts:
            for artifact in artifacts:
                lines.append(f"- Create the written status record at `{artifact}`.")
        elif artifacts:
            lines.append(
                "- Create or update "
                + ", ".join(f"`{artifact}`" for artifact in artifacts)
                + "."
            )
        elif item.read_only:
            lines.append("- Review the approved information and report the findings.")
        else:
            lines.append("- Make the approved change within the agreed project scope.")
    return lines or ["- Complete the approved work within the agreed project scope."]


def render_bestplan_failure(outcome: Mapping[str, Any] | None) -> str:
    """Render a concise host-owned explanation for a failed BestPlan run."""

    if not isinstance(outcome, Mapping):
        outcome = {}
    reason_code = str(outcome.get("reason_code") or "").casefold()
    if reason_code == "no_in_scope_implementation":
        return (
            "BestPlan unavailable\n\n"
            "- Reason: No executable implementation for this request fits "
            "within the active workspace.\n"
            "- Next step: Select the intended project workspace, or restate "
            "the task so it applies to the active workspace.\n\n"
            "- No plan was created or executed."
        )
    error = {
        "quorum_unavailable": "There were not enough usable planning results.",
        "provider_error": "One or more planning services failed.",
        "overall_timeout": "Planning did not finish within the allowed time.",
        "synthesizer_failed": "The final plan could not be written.",
        "credential_unavailable": "A required planning service was unavailable.",
    }.get(reason_code, "The planning run did not complete.")
    lines = [
        "BestPlan unavailable",
        "",
        f"- Reason: {error}",
    ]

    successes = outcome.get("successes")
    quorum = outcome.get("quorum")
    metadata = outcome.get("bestplan_receipt_metadata")
    attempts = metadata.get("attempts") if isinstance(metadata, Mapping) else None
    effective_count = (
        metadata.get("effective_count")
        if isinstance(metadata, Mapping)
        else None
    )
    if not isinstance(effective_count, int) or isinstance(effective_count, bool):
        effective_count = len(attempts) if isinstance(attempts, list) else None
    if (
        isinstance(successes, int)
        and not isinstance(successes, bool)
        and isinstance(quorum, int)
        and not isinstance(quorum, bool)
        and isinstance(effective_count, int)
        and not isinstance(effective_count, bool)
    ):
        lines.append(
            f"- Planning results: {successes} of {effective_count} were usable; "
            f"at least {quorum} were needed."
        )

    lines.extend(["", "Planning provenance"])
    lines.extend(_render_model_summary(metadata))
    lines.extend(["", "- No plan was created or executed."])
    return "\n".join(lines)


def _inline_text(value: Any) -> str:
    text = str(value or "").replace("`", "'")
    # Keep the human projection one-line and deterministic even if a trusted
    # source contains control characters.
    text = "".join(
        character if ord(character) >= 0x20 and ord(character) != 0x7F else " "
        for character in text
    )
    return " ".join(text.split())


def _bestplan_topic_from_invocation(value: Any) -> str:
    """Extract the user's task from the host-owned BestPlan invocation."""

    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    from .bestplan_orchestrator import TURN_MARKER, decode_bestplan_turn

    if text.startswith(TURN_MARKER):
        task, marker_config, marker_error = decode_bestplan_turn(text)
        if marker_config is None or marker_error is not None:
            return ""
        return _inline_text(task)

    match = re.match(r"^/(?:bestplan|bp)(?:\s|$)", text, re.IGNORECASE)
    if match is None:
        return ""
    task = text[match.end() :].strip()
    parts = task.split(maxsplit=1)
    if parts and parts[0].isdigit():
        task = parts[1] if len(parts) == 2 else ""
    return _inline_text(task)


def _executive_topic(value: Any) -> str:
    """Make a trusted task line understandable without exposing plan jargon."""

    topic = _inline_text(value)
    if not topic:
        return "The requested work"
    replacements = (
        ("Gate G0 HOLD receipt", "status record for a paused initial evidence check"),
        ("G0 HOLD receipt", "status record for a paused initial evidence check"),
        ("explicitly authorized in-workspace evidence", "approved evidence in the project folder"),
        ("explicitly authorized evidence paths", "approved evidence locations"),
        ("absence of explicitly authorized evidence", "missing approved evidence"),
        ("inferred current-state claims", "guesses about the live system"),
        ("current-state claims", "claims about the live system"),
        ("non-operational", "documentation-only"),
        ("non-executable", "for information only"),
        ("P1 proposal", "follow-up proposal"),
        ("Gate G0", "initial evidence check"),
        ("G0", "initial evidence check"),
        ("LaunchAgent", "macOS service"),
        ("launch-agent", "macOS service"),
        ("model routing", "AI model selection"),
        ("acceptance criteria", "required checks"),
        ("criteria", "requirements"),
        ("criterion", "requirement"),
        ("from only approved evidence", "using only approved evidence"),
        ("acceptance", "required checks"),
        ("receipt", "status record"),
        ("auditable", "traceable"),
        ("include a for information only follow-up proposal only if", "include a follow-up suggestion only if"),
    )
    for source, target in replacements:
        topic = re.sub(re.escape(source), target, topic, flags=re.IGNORECASE)
    return topic.rstrip(".!?") or "The requested work"


def _render_human_plan(
    plan: ExecutionPlan,
    *,
    workspace: str,
    plan_id: str,
    contract: Mapping[str, Any] | None,
    receipt_metadata: Mapping[str, Any] | None,
    topic: str | None = None,
) -> str:
    """Render the smallest human-readable approval summary from host truth."""

    hold = _is_evidence_hold_plan(plan)
    documentation_only = _is_documentation_only_plan(plan)
    lines = [
        "BestPlan plan",
        "",
        "Status",
        "- Plan-only — awaiting approval; no changes have started.",
        "",
        "Target",
        f"- `{_inline_text(workspace)}`.",
        "",
        "Topic",
        f"- {_executive_topic(topic)}.",
        "",
        "Decision",
    ]
    if hold:
        lines.append(
            "- Hold — there is not enough approved evidence to make a reliable "
            "statement about the current system."
        )
    else:
        lines.append("- Ready for approval — the scope and required checks are defined.")
    lines.append(f"- Risk: {_inline_text(plan.risk).capitalize()}.")
    lines.append("- No implementation or independent review has started.")

    lines.extend(["", "Proposed action"])
    lines.extend(_render_executive_actions(plan, hold=hold))
    if documentation_only:
        lines.append("- This is a documentation-only change.")
    elif all(item.read_only for item in plan.slices):
        lines.append("- No file changes are proposed.")

    lines.extend(["", "What will not change"])
    if hold or documentation_only:
        lines.append(
            "- No source code, settings, scheduled jobs, services, AI model "
            "selection, version history, or remote systems will change."
        )
    else:
        lines.append("- Nothing outside the approved files will change.")

    lines.extend(["", "Success condition"])
    if hold:
        lines.append(
            "- The written status record is complete and the required check passes. "
            "If evidence is still missing, the final status remains Hold."
        )
    else:
        lines.append(
            "- All required checks pass. If any check fails, Hermes stops before "
            "integrating changes."
        )

    lines.extend(["", "What could stop it"])
    lines.append(f"- {_inline_text(plan.stop_condition)}")

    local_contract = (
        isinstance(contract, Mapping)
        and contract.get("schema") == "hermes.bestplan.local-go.v1"
    )
    lines.extend(["", "After approval"])
    if local_contract:
        lines.extend([
            "- Hermes will run the approved work and checks.",
            "- Changes reach the local main branch only after every check passes.",
            "- Hermes will not publish to a remote system without separate approval.",
        ])
    else:
        lines.append("- Hermes will verify the approved scope before starting the work.")

    lines.extend(["", "Planning provenance"])
    lines.extend(_render_model_summary(receipt_metadata))
    lines.extend([
        "",
        "Approval",
        f"Bestplan executable receipt: {_inline_text(plan_id)}.",
        f"Reply with bare `go` to approve and dispatch plan `{_inline_text(plan_id)}`.",
    ])
    return "\n".join(lines)


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS bestplan_plans (
    plan_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    session_id TEXT,
    profile TEXT NOT NULL,
    workspace TEXT NOT NULL,
    baseline_revision TEXT,
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
    error TEXT,
    dispatch_id TEXT,
    dispatch_state TEXT,
    resolved_runtime_json TEXT,
    dispatch_owner TEXT,
    dispatch_started_at REAL,
    dispatch_updated_at REAL,
    sandbox_workspace TEXT,
    execution_protocol INTEGER NOT NULL DEFAULT 1,
    promotion_contract_version INTEGER,
    promotion_contract_json TEXT,
    promotion_contract_digest TEXT,
    promotion_mode TEXT,
    source_snapshot_json TEXT,
    source_snapshot_digest TEXT,
    current_phase TEXT,
    integration_oid TEXT,
    artifact_digest TEXT,
    candidate_set_digest TEXT,
    proof_authority_epoch TEXT,
    proof_event_seq INTEGER,
    proof_event_hash TEXT,
    verification_receipt_json TEXT,
    verification_receipt_digest TEXT,
    tests_verified_at REAL,
    review_verified_at REAL,
    remote_verified_at REAL,
    live_verified_at REAL,
    verified_at REAL,
    local_push_json TEXT,
    local_push_state TEXT,
    local_push_updated_at REAL,
    review_job_id TEXT,
    review_target_digest TEXT,
    review_receipt_digest TEXT
)
"""
_CREATE_INDEX_SQL = """CREATE INDEX IF NOT EXISTS idx_bestplan_plans_session_state
    ON bestplan_plans(session_id, state)"""


@dataclass(frozen=True)
class _ValidatedStoredPlan:
    execution_protocol: int
    plan: ExecutionPlan
    manifest: dict[str, Any]
    approval_digest: str
    contract: dict[str, Any] | None
    source_snapshot: Any | None


@dataclass(frozen=True)
class LandingRecoveryResult:
    """Observed outcome of one dead landing owner; Git is never replayed."""

    status: str


class _RepositoryEffectLock:
    """Closeable handle for one identity-bound repository directory lock."""

    def __init__(self, descriptor: int):
        self._descriptor = descriptor

    @property
    def closed(self) -> bool:
        return self._descriptor < 0

    def fileno(self) -> int:
        if self.closed:
            raise ValueError("repository effect lock is closed")
        return self._descriptor

    def close(self) -> None:
        descriptor = self._descriptor
        if descriptor < 0:
            return
        self._descriptor = -1
        os.close(descriptor)

    def __del__(self) -> None:
        try:
            self.close()
        except OSError:
            pass


def _open_repository_effect_lock(
    repo: object,
) -> tuple[str, _RepositoryEffectLock]:
    """Open the exact stored Git common directory as the effect lock inode."""

    path = getattr(repo, "common_dir", None)
    expected_device = getattr(repo, "common_dir_device", None)
    expected_inode = getattr(repo, "common_dir_inode", None)
    if (
        not isinstance(path, str)
        or not path
        or isinstance(expected_device, bool)
        or not isinstance(expected_device, int)
        or isinstance(expected_inode, bool)
        or not isinstance(expected_inode, int)
    ):
        raise BestplanError("repository effect lock identity is invalid")
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or (opened.st_dev, opened.st_ino)
            != (expected_device, expected_inode)
        ):
            raise BestplanError("repository effect lock identity changed")
        handle = _RepositoryEffectLock(descriptor)
        descriptor = -1
        return path, handle
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_stored_plan_row(
    row: sqlite3.Row | dict[str, Any],
    *,
    allow_v1_null_approval: bool = False,
) -> _ValidatedStoredPlan:
    """Revalidate every immutable approval input from one stored row.

    The contract and source are never reconstructed from current config.  This
    pure validator is called while each state-transition transaction owns its
    write lock and is also used by the pre-dispatch host path.
    """

    values = dict(row)
    if values.get("version") != 1 or isinstance(values.get("version"), bool):
        raise BestplanError("stored model envelope version must remain integer 1")
    try:
        _raw_envelope, plan, raw_manifest = _extract_envelope(values["raw_plan_json"])
        stored_manifest = compile_execution_plan(
            json.loads(values["validated_manifest_json"])
        ).to_manifest()
    except Exception as exc:
        raise BestplanError(f"stored plan envelope/manifest is invalid: {exc}") from exc
    if raw_manifest != stored_manifest:
        raise BestplanError("raw envelope and validated manifest differ")

    protocol = values.get("execution_protocol", 1)
    if protocol not in (1, 2) or isinstance(protocol, bool):
        raise BestplanError("stored execution_protocol must be integer 1 or 2")

    source_raw = values.get("source_snapshot_json")
    source_digest_raw = values.get("source_snapshot_digest")
    if (source_raw is None) != (source_digest_raw is None):
        raise BestplanError("stored source snapshot is partial")
    snapshot = None
    if source_raw is not None:
        try:
            snapshot = source_snapshot_from_json(source_raw)
        except Exception as exc:
            raise BestplanError(f"stored source snapshot is invalid: {exc}") from exc
        if source_snapshot_digest(snapshot) != source_digest_raw:
            raise BestplanError("stored source snapshot digest differs")
        if values.get("baseline_fingerprint") != snapshot.fingerprint:
            raise BestplanError("stored source fingerprint differs from baseline")
        if values.get("baseline_revision") != snapshot.head_oid:
            raise BestplanError("stored source revision differs from baseline")
        if values.get("workspace") != snapshot.repo.workspace:
            raise BestplanError("stored source workspace identity differs")

    contract: dict[str, Any] | None = None
    if protocol == 1:
        if any(
            values.get(name) is not None
            for name in (
                "promotion_contract_version",
                "promotion_contract_json",
                "promotion_contract_digest",
                "promotion_mode",
            )
        ):
            raise BestplanError("protocol-1 row contains a promotion contract")
        expected_digest = _manifest_digest(stored_manifest)
        stored_approval = values.get("approval_digest")
        if stored_approval is None and allow_v1_null_approval:
            pass
        elif stored_approval != expected_digest:
            raise BestplanError("approval digest does not match protocol-1 manifest")
    else:
        if snapshot is None:
            raise BestplanError("protocol-2 row has no source snapshot")
        raw_contract = values.get("promotion_contract_json")
        stored_contract_digest = values.get("promotion_contract_digest")
        if not isinstance(raw_contract, str) or not isinstance(stored_contract_digest, str):
            raise BestplanError("protocol-2 row has incomplete contract storage")
        contract_version = values.get("promotion_contract_version")
        if contract_version == 1 and not isinstance(contract_version, bool):
            try:
                from .bestplan_local import (
                    LOCAL_GO_CONTRACT_SCHEMA,
                    local_go_approval_digest,
                    local_go_contract_digest,
                    local_go_contract_json,
                    local_go_manifest_digest,
                    validate_local_go_contract,
                )

                decoded_contract = json.loads(raw_contract)
                if not isinstance(decoded_contract, Mapping) or decoded_contract.get(
                    "schema"
                ) != LOCAL_GO_CONTRACT_SCHEMA:
                    raise ContractValidationError(
                        "local-go contract schema is unsupported"
                    )
                contract = validate_local_go_contract(decoded_contract)
                if local_go_contract_json(contract) != raw_contract:
                    raise ContractValidationError("contract JSON is not canonical")
            except Exception as exc:
                raise BestplanError(
                    f"stored local-go contract is invalid: {exc}"
                ) from exc
            if local_go_contract_digest(contract) != stored_contract_digest:
                raise BestplanError("stored local-go contract digest differs")
            if values.get("promotion_mode") != contract["mode"]:
                raise BestplanError("stored local-go mode differs from contract")
            if contract["manifest_digest"] != local_go_manifest_digest(
                stored_manifest
            ):
                raise BestplanError("local-go contract manifest digest differs")
            source = contract["source"]
            if source["snapshot_digest"] != source_digest_raw:
                raise BestplanError("contract/source snapshot digest differs")
            if source["source_digest"] != snapshot.fingerprint:
                raise BestplanError("contract/source fingerprint differs")
            if source["protected_digest"] != snapshot.protected_manifest.digest:
                raise BestplanError("contract/protected manifest digest differs")
            if (
                source["base_oid"] != snapshot.head_oid
                or source["local_ref"] != LOCAL_PUSH_REF
            ):
                raise BestplanError("contract/source base object differs")
            if source["tree_oid"] != snapshot.tree_oid:
                raise BestplanError("contract/source tree object differs")
            if contract["repository"] != source_snapshot_to_dict(snapshot)["repository"]:
                raise BestplanError("contract/source repository identity differs")
            expected_digest = local_go_approval_digest(stored_manifest, contract)
            if values.get("approval_digest") != expected_digest:
                raise BestplanError(
                    "approval digest does not match manifest and local-go contract"
                )
        else:
            # Preserve the existing controlled-promotion V2 path byte-for-byte.
            if contract_version != 2 or isinstance(contract_version, bool):
                raise BestplanError("protocol-2 row has no contract version 2")
            try:
                decoded_contract = json.loads(raw_contract)
                contract = validate_execution_contract(decoded_contract)
                if contract_json(contract) != raw_contract:
                    raise ContractValidationError("contract JSON is not canonical")
            except Exception as exc:
                raise BestplanError(f"stored promotion contract is invalid: {exc}") from exc
            if contract_digest(contract) != stored_contract_digest:
                raise BestplanError("stored promotion contract digest differs")
            if values.get("promotion_mode") != contract["promotion_mode"]:
                raise BestplanError("stored promotion mode differs from contract")
            source = contract["source"]
            if source["snapshot_digest"] != source_digest_raw:
                raise BestplanError("contract/source snapshot digest differs")
            if source["source_digest"] != snapshot.fingerprint:
                raise BestplanError("contract/source fingerprint differs")
            if source["protected_digest"] != snapshot.protected_manifest.digest:
                raise BestplanError("contract/protected manifest digest differs")
            if source["base_oid"] != snapshot.head_oid or source["local_main_oid"] != snapshot.head_oid:
                raise BestplanError("contract/source base object differs")
            if source["tree_oid"] != snapshot.tree_oid:
                raise BestplanError("contract/source tree object differs")
            if contract["repository"] != source_snapshot_to_dict(snapshot)["repository"]:
                raise BestplanError("contract/source repository identity differs")
            expected_digest = _approval_digest(stored_manifest, contract)
            if values.get("approval_digest") != expected_digest:
                raise BestplanError("approval digest does not match manifest and contract")

    return _ValidatedStoredPlan(
        execution_protocol=protocol,
        plan=plan,
        manifest=stored_manifest,
        approval_digest=expected_digest,
        contract=contract,
        source_snapshot=snapshot,
    )


class BestplanStore:
    """SQLite authority for immutable plan envelopes and atomic claims."""

    def __init__(
        self,
        session_db=None,
        db_path: Optional[Path] = None,
        *,
        reconcile_push_state: bool = True,
    ):
        self._session_db = session_db
        self._db_path = Path(db_path) if db_path is not None else None
        self._lock = threading.RLock()
        self._owned_connection: sqlite3.Connection | None = None
        if self._session_db is None and self._db_path is not None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._owned_connection = sqlite3.connect(
                str(self._db_path), check_same_thread=False, timeout=30,
            )
            self._owned_connection.row_factory = sqlite3.Row
            journal_deadline = time.monotonic() + 30.0
            while True:
                try:
                    journal_mode = self._owned_connection.execute(
                        "PRAGMA journal_mode=WAL"
                    ).fetchone()[0]
                    if str(journal_mode).casefold() != "wal":
                        raise sqlite3.OperationalError(
                            f"could not enable WAL journal mode: {journal_mode}"
                        )
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).casefold() or time.monotonic() >= journal_deadline:
                        raise
                    time.sleep(0.01)
            self._owned_connection.execute("PRAGMA synchronous=FULL")
            self._owned_connection.execute(_CREATE_TABLE_SQL)
            self._owned_connection.execute(_CREATE_INDEX_SQL)
            self._owned_connection.commit()
        self._ensure_schema()
        self.reconcile_async_tracker()
        if reconcile_push_state:
            self.reconcile_local_pushes()

    def close(self) -> None:
        with self._lock:
            if self._owned_connection is not None:
                self._owned_connection.close()
                self._owned_connection = None

    @property
    def state_db_path(self) -> Path | None:
        """Return the durable locator without exposing the open connection."""

        if self._db_path is not None:
            return self._db_path
        session_path = getattr(self._session_db, "db_path", None)
        return Path(session_path) if session_path is not None else None

    def _connection(self) -> sqlite3.Connection:
        if self._session_db is not None:
            return self._session_db._conn
        if self._owned_connection is not None:
            return self._owned_connection
        from hermes_state import SessionDB
        self._session_db = SessionDB()
        return self._session_db._conn

    def _execute_write(self, fn: Callable[[sqlite3.Connection], Any]):
        if self._session_db is not None:
            return self._session_db._execute_write(fn)
        with self._lock:
            conn = self._connection()
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    def _ensure_schema(self) -> None:
        columns = {
            "baseline_revision": "TEXT",
            "dispatch_id": "TEXT",
            "dispatch_state": "TEXT",
            "resolved_runtime_json": "TEXT",
            "dispatch_owner": "TEXT",
            "dispatch_started_at": "REAL",
            "dispatch_updated_at": "REAL",
            "sandbox_workspace": "TEXT",
            "execution_protocol": "INTEGER NOT NULL DEFAULT 1",
            "promotion_contract_version": "INTEGER",
            "promotion_contract_json": "TEXT",
            "promotion_contract_digest": "TEXT",
            "promotion_mode": "TEXT",
            "source_snapshot_json": "TEXT",
            "source_snapshot_digest": "TEXT",
            "current_phase": "TEXT",
            "integration_oid": "TEXT",
            "artifact_digest": "TEXT",
            "candidate_set_digest": "TEXT",
            "proof_authority_epoch": "TEXT",
            "proof_event_seq": "INTEGER",
            "proof_event_hash": "TEXT",
            "verification_receipt_json": "TEXT",
            "verification_receipt_digest": "TEXT",
            "tests_verified_at": "REAL",
            "review_verified_at": "REAL",
            "remote_verified_at": "REAL",
            "live_verified_at": "REAL",
            "verified_at": "REAL",
            "local_push_json": "TEXT",
            "local_push_state": "TEXT",
            "local_push_updated_at": "REAL",
            "review_job_id": "TEXT",
            "review_target_digest": "TEXT",
            "review_receipt_digest": "TEXT",
        }

        def migrate(conn):
            # executescript() commits implicitly.  Keep schema creation and
            # every additive ALTER inside the caller's BEGIN IMMEDIATE.
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            existing = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(bestplan_plans)")
            }
            for name, sql_type in columns.items():
                if name not in existing:
                    try:
                        conn.execute(
                            f"ALTER TABLE bestplan_plans ADD COLUMN {name} {sql_type}"
                        )
                    except sqlite3.OperationalError:
                        # Concurrent openers serialize on BEGIN IMMEDIATE.  If
                        # another connection nevertheless completed this same
                        # additive column first, accept only that exact state.
                        refreshed = {
                            str(row[1])
                            for row in conn.execute("PRAGMA table_info(bestplan_plans)")
                        }
                        if name not in refreshed:
                            raise

            # Task-2 protocol rows predate the relational promotion-mode
            # projection.  Backfill only from their already-persisted,
            # canonical contract; incomplete legacy rows remain non-authoritative.
            rows = conn.execute(
                "SELECT plan_id, promotion_contract_json FROM bestplan_plans "
                "WHERE execution_protocol=2 AND promotion_mode IS NULL"
            ).fetchall()
            for row in rows:
                raw_contract = row["promotion_contract_json"]
                if not isinstance(raw_contract, str):
                    continue
                try:
                    migrated_contract = validate_execution_contract(
                        json.loads(raw_contract)
                    )
                except Exception:
                    continue
                conn.execute(
                    "UPDATE bestplan_plans SET promotion_mode=? "
                    "WHERE plan_id=? AND promotion_mode IS NULL",
                    (migrated_contract["promotion_mode"], row["plan_id"]),
                )

            from .bestplan_proof import install_proof_schema

            install_proof_schema(conn)
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_bestplan_review_binding_immutable
                BEFORE UPDATE OF review_job_id, review_target_digest,
                    review_receipt_digest ON bestplan_plans
                WHEN OLD.review_job_id IS NOT NULL AND (
                    NEW.review_job_id IS NOT OLD.review_job_id
                    OR NEW.review_target_digest IS NOT OLD.review_target_digest
                    OR NEW.review_receipt_digest IS NOT OLD.review_receipt_digest
                )
                BEGIN
                    SELECT RAISE(ABORT, 'bestplan review binding is immutable');
                END
                """
            )

        self._execute_write(migrate)

    def _read_lock(self):
        return getattr(self._session_db, "_lock", self._lock)

    def reconcile_async_tracker(self) -> int:
        """Reconcile plan rows with the deterministic async tracker at startup."""
        state_path = self.state_db_path
        if state_path is None:
            return 0
        tracker = state_path.parent / "async_delegations.json"
        try:
            raw = json.loads(tracker.read_text(encoding="utf-8"))
            records = raw.get("records") or {}
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return 0
        if not isinstance(records, dict):
            return 0
        from tools.async_delegation import _owner_liveness

        def reconcile(conn):
            def exact_pending_execution_owner(
                plan_row: sqlite3.Row,
                delegation_id: str,
                record: dict[str, Any],
            ) -> tuple[bool, dict[str, Any] | None]:
                """Return whether a pipeline exists and its exact valid owner."""

                try:
                    from .bestplan_local import local_go_manifest_digest

                    pipeline = conn.execute(
                        "SELECT * FROM bestplan_execution_pipelines "
                        "WHERE plan_id=? AND delegation_id=?",
                        (plan_row["plan_id"], delegation_id),
                    ).fetchone()
                    if pipeline is None:
                        return False, None
                    intent = json.loads(str(pipeline["adapter_state_json"]))
                    routes = json.loads(str(pipeline["runtime_routes_json"]))
                    planned_runtimes = json.loads(
                        str(plan_row["resolved_runtime_json"] or "[]")
                    )
                    validated = _validate_stored_plan_row(plan_row)
                    candidate_count = pipeline["candidate_count"]
                    next_ordinal = pipeline["next_attempt_ordinal"]
                    active_ordinal = pipeline["active_attempt_ordinal"]
                    attempt_owner_pid = pipeline["attempt_owner_pid"]
                    attempt_owner_start = pipeline[
                        "attempt_owner_process_start_id"
                    ]
                    raw_request = intent.get("raw_request")
                    if not isinstance(raw_request, str):
                        return None
                    request_bytes = raw_request.encode("utf-8")
                except (
                    KeyError,
                    TypeError,
                    ValueError,
                    UnicodeError,
                    sqlite3.DatabaseError,
                    json.JSONDecodeError,
                ):
                    return True, None
                if (
                    not isinstance(intent, dict)
                    or not isinstance(routes, list)
                    or not all(isinstance(item, dict) for item in routes)
                    or not isinstance(planned_runtimes, list)
                    or not all(
                        isinstance(item, dict) for item in planned_runtimes
                    )
                    or isinstance(candidate_count, bool)
                    or not isinstance(candidate_count, int)
                    or candidate_count <= 0
                    or isinstance(next_ordinal, bool)
                    or not isinstance(next_ordinal, int)
                    or next_ordinal < 0
                    or len(planned_runtimes) != candidate_count
                    or len(routes) != candidate_count + 2
                ):
                    return True, None
                candidate_routes = routes[:candidate_count]
                review_routes = routes[candidate_count:]
                candidate_routes_match = all(
                    all(
                        planned.get(field) == durable.get(field)
                        for field in (
                            "provider", "model", "runtime_fingerprint",
                        )
                    )
                    for planned, durable in zip(
                        planned_runtimes, candidate_routes, strict=True,
                    )
                )
                reviewer_routes_valid = all(
                    isinstance(item.get("provider"), str)
                    and bool(item["provider"])
                    and isinstance(item.get("model"), str)
                    and bool(item["model"])
                    and isinstance(item.get("runtime_fingerprint"), str)
                    and bool(re.fullmatch(
                        r"[0-9a-f]{64}", item["runtime_fingerprint"],
                    ))
                    for item in review_routes
                )
                owner_is_unclaimed = (
                    attempt_owner_pid is None
                    and attempt_owner_start is None
                    and active_ordinal is None
                )
                owner_is_claimed = (
                    not isinstance(attempt_owner_pid, bool)
                    and isinstance(attempt_owner_pid, int)
                    and attempt_owner_pid > 0
                    and isinstance(attempt_owner_start, str)
                    and bool(attempt_owner_start.strip())
                    and len(attempt_owner_start) <= 256
                    and not isinstance(active_ordinal, bool)
                    and isinstance(active_ordinal, int)
                    and active_ordinal >= 0
                    and next_ordinal == active_ordinal + 1
                )
                if not owner_is_unclaimed and not owner_is_claimed:
                    return True, None
                exact_execution = bool(
                    pipeline["state"] == "pending"
                    and pipeline["job_id"]
                    == record.get("bestplan_review_job_id")
                    and pipeline["owner_session_id"]
                    == plan_row["session_id"]
                    == record.get("origin_session_id")
                    and pipeline["owner_profile"]
                    == plan_row["profile"]
                    == record.get("origin_profile")
                    and pipeline["workspace"] == plan_row["workspace"]
                    and pipeline["adapter_version"]
                    == "local-bestplan-execution.v1"
                    and record.get("bestplan_local_execution") is True
                    and record.get("bestplan_plan_id") == plan_row["plan_id"]
                    and record.get("bestplan_state_db_path")
                    == str(state_path.resolve())
                    and record.get("origin_tracker_path")
                    == str(tracker.resolve())
                    and validated.execution_protocol == 2
                    and validated.source_snapshot is not None
                    and isinstance(validated.contract, Mapping)
                    and validated.contract.get("schema")
                    == "hermes.bestplan.local-go.v1"
                    and validated.contract.get("mode") == "local_main"
                    and candidate_count == len(validated.plan.slices)
                    and set(intent) == {
                        "approval_digest",
                        "contract_digest",
                        "manifest_digest",
                        "raw_request",
                        "raw_request_sha256",
                        "schema",
                        "source_snapshot_digest",
                    }
                    and intent.get("schema")
                    == "hermes.bestplan.execution-intent.v1"
                    and intent.get("approval_digest")
                    == validated.approval_digest
                    and intent.get("contract_digest")
                    == plan_row["promotion_contract_digest"]
                    and intent.get("manifest_digest")
                    == local_go_manifest_digest(validated.manifest)
                    and intent.get("source_snapshot_digest")
                    == source_snapshot_digest(validated.source_snapshot)
                    and bool(raw_request.strip())
                    and b"\x00" not in request_bytes
                    and len(request_bytes) <= 256 * 1024
                    and intent.get("raw_request_sha256")
                    == hashlib.sha256(request_bytes).hexdigest()
                    and candidate_routes_match
                    and reviewer_routes_valid
                    and [item.get("route") for item in candidate_routes]
                    == [
                        f"candidate-{index}"
                        for index in range(candidate_count)
                    ]
                    and [item.get("route") for item in review_routes]
                    == ["smart_reviewer", "code_worker"]
                )
                if not exact_execution:
                    return True, None
                if owner_is_unclaimed:
                    return True, {}
                return True, {
                    "owner_pid": attempt_owner_pid,
                    "owner_started_at": attempt_owner_start,
                }

            changed = 0
            rows = conn.execute(
                "SELECT * FROM bestplan_plans "
                "WHERE state IN (?, ?)",
                (PlanState.RUNNING, PlanState.WAITING),
            ).fetchall()
            for row in rows:
                delegation_id = str(row["dispatch_id"] or f"bestplan-{row['plan_id']}")
                entry = records.get(delegation_id)
                if not isinstance(entry, dict):
                    continue
                record = entry.get("record") if isinstance(entry.get("record"), dict) else {}
                phase = str(entry.get("status") or record.get("status") or "")
                event = entry.get("event") if isinstance(entry.get("event"), dict) else None
                if int(row["execution_protocol"] or 1) == 2:
                    from .bestplan_proof import ProofLedger
                    from .bestplan_redaction import RedactionError

                    advisory_kind: str | None = None
                    compatibility_error: str | None = None
                    advisory_output: Any = entry
                    dispatch_state: str | None = None
                    clear_owner = False
                    tracker_is_terminal = bool(event) or phase in {
                        "completed",
                        "error",
                        "failed",
                        "lost",
                        "interrupted",
                    }
                    if phase in {"scheduled", "running"}:
                        exact_owner: dict[str, Any] | None = None
                        pipeline_present = False
                        if phase == "running":
                            pipeline_present, exact_owner = exact_pending_execution_owner(
                                row, delegation_id, record,
                            )
                            owner_live = (
                                _owner_liveness(exact_owner)
                                if pipeline_present and exact_owner
                                else (
                                    False
                                    if pipeline_present
                                    else _owner_liveness(record)
                                )
                            )
                        else:
                            owner_live = _owner_liveness(record)
                        if owner_live is None:
                            continue
                        if owner_live:
                            advisory_kind = "async_tracker_running_advisory"
                            dispatch_state = "scheduled"
                        elif phase == "scheduled":
                            advisory_kind = "async_tracker_recovered_advisory"
                            compatibility_error = "recovered_pre_run_schedule"
                            dispatch_state = "intent"
                            clear_owner = True
                        elif pipeline_present and exact_owner is not None:
                            # The immutable execution intent, not this
                            # compatibility projection, owns pre-review crash
                            # recovery. The async recovery queue will prove the
                            # same identities and claim a fresh attempt.
                            continue
                        else:
                            advisory_kind = "async_tracker_lost_advisory"
                            compatibility_error = "recapture_required"
                            dispatch_state = "terminal"
                            clear_owner = True
                            advisory_output = {
                                "delegation_id": delegation_id,
                                "status": "lost",
                                "error": "async delegation owner exited during running phase",
                            }
                    elif tracker_is_terminal:
                        advisory_kind = "async_tracker_terminal_advisory"
                        compatibility_error = "recapture_required"
                        dispatch_state = "terminal"
                        advisory_output = event or {
                            "delegation_id": delegation_id,
                            "status": phase or "terminal",
                            "result": entry.get("result"),
                        }
                    authority_phase = str(row["current_phase"] or "captured")
                    captured_is_terminal = (
                        authority_phase == "captured"
                        and str(row["dispatch_state"] or "") == "terminal"
                    )
                    if captured_is_terminal:
                        compatibility_error = None
                        dispatch_state = None
                        clear_owner = False
                    elif authority_phase != "captured":
                        compatibility_error = None
                        clear_owner = False
                        if authority_phase == "candidate_ready" and tracker_is_terminal:
                            dispatch_state = "terminal"
                            clear_owner = True
                        else:
                            dispatch_state = None
                    if advisory_kind is not None:
                        try:
                            ProofLedger(self).append_advisory_in_transaction(
                                conn,
                                plan_id=row["plan_id"],
                                kind=advisory_kind,
                                raw_output=advisory_output,
                                output_source="async",
                                compatibility_error=compatibility_error,
                                compatibility_dispatch_state=dispatch_state,
                                compatibility_clear_dispatch_owner=clear_owner,
                            )
                        except RedactionError:
                            ProofLedger(self).append_advisory_in_transaction(
                                conn,
                                plan_id=row["plan_id"],
                                kind=advisory_kind,
                                raw_output={
                                    "code": "tracker_payload_rejected",
                                    "status": "recapture_required",
                                },
                                output_source="async",
                                compatibility_error=compatibility_error,
                                compatibility_dispatch_state=dispatch_state,
                                compatibility_clear_dispatch_owner=clear_owner,
                            )
                        changed += 1
                    continue
                if phase in {"scheduled", "running"}:
                    owner_live = _owner_liveness(record)
                    if owner_live is None:
                        continue
                    if owner_live:
                        changed += conn.execute(
                            "UPDATE bestplan_plans SET state=?, dispatch_state='scheduled', "
                            "dispatch_updated_at=? WHERE plan_id=? AND state=?",
                            (PlanState.WAITING, time.time(), row["plan_id"], PlanState.RUNNING),
                        ).rowcount
                    elif phase == "scheduled":
                        changed += conn.execute(
                            "UPDATE bestplan_plans SET state=?, dispatch_state='intent', "
                            "dispatch_owner=NULL, dispatch_updated_at=?, "
                            "error='recovered_pre_run_schedule' "
                            "WHERE plan_id=? AND state IN (?, ?)",
                            (
                                PlanState.RUNNING,
                                time.time(),
                                row["plan_id"],
                                PlanState.RUNNING,
                                PlanState.WAITING,
                            ),
                        ).rowcount
                    else:
                        evidence = {
                            "delegation_id": delegation_id,
                            "status": "lost",
                            "error": "async delegation owner exited during running phase",
                        }
                        now = time.time()
                        changed += conn.execute(
                            "UPDATE bestplan_plans SET state=?, dispatch_state='terminal', "
                            "evidence_json=?, completed_at=COALESCE(completed_at, ?), "
                            "dispatch_updated_at=?, error=? "
                            "WHERE plan_id=? AND state IN (?, ?)",
                            (
                                PlanState.COMPLETED_UNVERIFIED,
                                json.dumps(evidence, sort_keys=True),
                                now,
                                now,
                                evidence["error"],
                                row["plan_id"],
                                PlanState.RUNNING,
                                PlanState.WAITING,
                            ),
                        ).rowcount
                elif phase in {"completed", "error", "failed", "lost", "interrupted"} or event:
                    evidence = event or {
                        "delegation_id": delegation_id,
                        "status": phase or "terminal",
                        "result": entry.get("result"),
                    }
                    changed += conn.execute(
                        "UPDATE bestplan_plans SET state=?, dispatch_state='terminal', "
                        "evidence_json=?, completed_at=COALESCE(completed_at, ?), "
                        "dispatch_updated_at=? WHERE plan_id=? AND state IN (?, ?)",
                        (
                            PlanState.COMPLETED_UNVERIFIED,
                            json.dumps(evidence, sort_keys=True), time.time(), time.time(),
                            row["plan_id"], PlanState.RUNNING, PlanState.WAITING,
                        ),
                    ).rowcount
            return changed

        return int(self._execute_write(reconcile))

    def create_plan(
        self,
        raw_request: str,
        plan: ExecutionPlan,
        *,
        session_id: str,
        workspace: str,
        profile: Optional[str] = None,
        baseline_fingerprint: Optional[str] = None,
        raw_envelope: Optional[str] = None,
        provisional: bool = False,
        config: Optional[dict[str, Any]] = None,
        authority_client: BestplanAuthorityClient | None = None,
        local_execution: bool = False,
    ) -> str:
        if type(local_execution) is not bool:
            raise BestplanError("local_execution must be true or false")
        workspace = _workspace_hint(workspace)
        supplied_fingerprint = (
            None if baseline_fingerprint is None else str(baseline_fingerprint)
        )
        baseline_revision: str | None = None
        execution_protocol = 1
        source_json_value: str | None = None
        source_digest_value: str | None = None
        contract_value: dict[str, Any] | None = None
        contract_json_value: str | None = None
        contract_digest_value: str | None = None
        manifest = plan.to_manifest()
        if not strong_source_capture_supported():
            if local_execution:
                raise BaselineFingerprintError(
                    "local BestPlan requires strong Git source capture"
                )
            try:
                workspace, captured_fingerprint = capture_legacy_v1_fingerprint(
                    workspace,
                    time.monotonic() + DEFAULT_SOURCE_OPERATION_SECONDS,
                )
            except SourceBoundaryError as exc:
                if not supplied_fingerprint:
                    raise BaselineFingerprintError(
                        "candidate-only legacy git baseline unavailable for "
                        f"{workspace}: {exc.code}: {exc}"
                    ) from exc
                try:
                    workspace, has_git_boundary = inspect_legacy_v1_workspace(
                        workspace,
                        time.monotonic() + DEFAULT_SOURCE_OPERATION_SECONDS,
                    )
                except SourceBoundaryError as inspection_exc:
                    raise BaselineFingerprintError(
                        "candidate-only legacy git baseline unavailable for "
                        f"{workspace}: {inspection_exc.code}: {inspection_exc}"
                    ) from inspection_exc
                if has_git_boundary:
                    raise BaselineFingerprintError(
                        "candidate-only legacy git baseline unavailable for "
                        f"{workspace}: {exc.code}: {exc}"
                    ) from exc
                baseline_fingerprint = supplied_fingerprint
            else:
                if (
                    supplied_fingerprint is not None
                    and supplied_fingerprint != captured_fingerprint
                ):
                    raise BaselineFingerprintError(
                        "supplied baseline fingerprint does not match the "
                        f"candidate-only legacy source proof for {workspace}"
                    )
                # Legacy rows deliberately have no revision. Task 2 keeps
                # baseline_revision=NULL plans candidate-only/non-executable.
                baseline_fingerprint = captured_fingerprint
        else:
            try:
                repo = resolve_repo_identity(workspace)
            except SourceBoundaryError as exc:
                if local_execution:
                    raise BaselineFingerprintError(
                        f"strong git baseline unavailable for {workspace}: "
                        f"{exc.code}: {exc}"
                    ) from exc
                if not supplied_fingerprint:
                    raise BaselineFingerprintError(
                        f"strong git baseline unavailable for {workspace}: {exc.code}: {exc}"
                    ) from exc
                try:
                    workspace, has_git_boundary = inspect_workspace_boundary(
                        workspace,
                        time.monotonic() + DEFAULT_SOURCE_OPERATION_SECONDS,
                    )
                except SourceBoundaryError as inspection_exc:
                    raise BaselineFingerprintError(
                        "strong git baseline unavailable for "
                        f"{workspace}: {inspection_exc.code}: {inspection_exc}"
                    ) from inspection_exc
                if has_git_boundary:
                    raise BaselineFingerprintError(
                        f"strong git baseline unavailable for {workspace}: "
                        f"{exc.code}: {exc}"
                    ) from exc
                # Compatibility only for historical tests/callers that inject a
                # synthetic baseline for a nonexistent or non-Git workspace.
                # Task 2 gates trusted V2 execution on baseline_revision != NULL.
                baseline_fingerprint = supplied_fingerprint
            else:
                workspace = repo.workspace
                enrollment = None
                if not local_execution:
                    try:
                        enrollment = resolve_matching_enrollment(
                            config or {}, repo, authority_client,
                        )
                    except Exception as exc:
                        raise BestplanError(
                            f"matching promotion enrollment is invalid: {exc}"
                        ) from exc
                capture_seconds = (
                    enrollment.capture_budget_seconds
                    if enrollment is not None
                    else DEFAULT_SOURCE_OPERATION_SECONDS
                )
                try:
                    snapshot = capture_source_snapshot(
                        repo, time.monotonic() + capture_seconds,
                    )
                except SourceBoundaryError as exc:
                    raise BaselineFingerprintError(
                        f"strong git baseline unavailable for {workspace}: {exc.code}: {exc}"
                    ) from exc
                if (
                    supplied_fingerprint is not None
                    and supplied_fingerprint != snapshot.fingerprint
                ):
                    raise BaselineFingerprintError(
                        "supplied baseline fingerprint does not match the trusted "
                        f"source snapshot for {workspace}"
                    )
                baseline_fingerprint = snapshot.fingerprint
                baseline_revision = snapshot.head_oid
                source_json_value = source_snapshot_json(snapshot)
                source_digest_value = source_snapshot_digest(snapshot)
                if local_execution:
                    try:
                        from .bestplan_local import (
                            build_local_go_contract,
                            capture_local_execution_inputs,
                            local_go_contract_digest,
                            local_go_contract_json,
                            local_go_manifest_digest,
                        )

                        local_inputs = capture_local_execution_inputs(
                            snapshot=snapshot,
                            controller_python=Path(sys.executable),
                            manifest=manifest,
                        )
                        contract_value = build_local_go_contract(
                            snapshot=snapshot,
                            controller=local_inputs.controller,
                            commands=local_inputs.check_plan.commands,
                            manifest_digest=local_go_manifest_digest(manifest),
                            check_runtime_digest=(
                                local_inputs.check_plan.check_runtime_digest
                            ),
                        )
                        contract_json_value = local_go_contract_json(
                            contract_value
                        )
                        contract_digest_value = local_go_contract_digest(
                            contract_value
                        )
                    except Exception as exc:
                        raise BestplanError(
                            f"local BestPlan runtime capture failed: {exc}"
                        ) from exc
                    execution_protocol = 2
                elif enrollment is not None:
                    try:
                        contract_value = build_execution_contract(
                            plan, snapshot, enrollment, enrollment.controller,
                        )
                        contract_json_value = contract_json(contract_value)
                        contract_digest_value = contract_digest(contract_value)
                    except ContractValidationError as exc:
                        raise BestplanError(
                            f"matching promotion enrollment cannot issue a contract: {exc}"
                        ) from exc
                    execution_protocol = 2
        manifest_json = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
        if raw_envelope is None:
            raw = (
                f"{BESTPLAN_ENVELOPE_START}\n"
                + json.dumps(
                    {"version": 1, "manifest": manifest},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + f"\n{BESTPLAN_ENVELOPE_END}"
            )
        else:
            raw = str(raw_envelope)
        plan_id = f"bp_{uuid.uuid4().hex}"
        if local_execution:
            from .bestplan_local import local_go_approval_digest

            digest = local_go_approval_digest(manifest, contract_value or {})
        else:
            digest = _approval_digest(manifest, contract_value)
        contract_version_value = (
            1 if local_execution else 2 if execution_protocol == 2 else None
        )
        promotion_mode_value = None
        if contract_value is not None:
            promotion_mode_value = (
                contract_value.get("mode")
                if local_execution
                else contract_value.get("promotion_mode")
            )

        def insert(conn):
            conn.execute(
                """INSERT INTO bestplan_plans (
                    plan_id, version, created_at, session_id, profile, workspace,
                    baseline_revision, baseline_fingerprint, raw_request, raw_plan_json,
                    validated_manifest_json, state, approval_digest, execution_protocol,
                    promotion_contract_version, promotion_contract_json,
                    promotion_contract_digest, promotion_mode, source_snapshot_json,
                    source_snapshot_digest, current_phase
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    plan_id, time.time(), str(session_id),
                    str(_active_profile() if profile is None else profile), workspace,
                    baseline_revision, baseline_fingerprint,
                    str(raw_request or ""), raw, manifest_json,
                    PlanState.PROVISIONAL if provisional else PlanState.PENDING,
                    digest, execution_protocol,
                    contract_version_value,
                    contract_json_value, contract_digest_value,
                    promotion_mode_value,
                    source_json_value, source_digest_value,
                    "captured" if execution_protocol == 2 else None,
                ),
            )

        self._execute_write(insert)
        return plan_id

    def get_plan(self, plan_id: str) -> Optional[dict[str, Any]]:
        with self._read_lock():
            row = self._connection().execute(
                "SELECT * FROM bestplan_plans WHERE plan_id = ?", (plan_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def _session_matches_visible_continuation(
        self,
        stored_session_id: str,
        visible_session_id: str,
    ) -> bool:
        """Match an immutable owner to its exact compression continuation."""

        stored = str(stored_session_id)
        visible = str(visible_session_id)
        if stored == visible:
            return True
        session_db = self._session_db
        resolver = getattr(session_db, "resolve_resume_session_id", None)
        if not callable(resolver):
            return False
        try:
            return str(resolver(stored)) == visible
        except Exception:
            return False

    def list_for_session(self, session_id: str, *, open_only: bool = True) -> list[dict[str, Any]]:
        visible_session_id = str(session_id)
        sql = (
            "SELECT * FROM bestplan_plans WHERE "
            "(session_id = ? OR (execution_protocol=2 "
            "AND promotion_contract_version=1 "
            "AND promotion_mode='local_main'))"
        )
        params: list[Any] = [visible_session_id]
        if open_only:
            sql += " AND state IN (?, ?, ?, ?)"
            params.extend(_OPEN_STATES)
        sql += " ORDER BY created_at DESC"
        with self._read_lock():
            rows = self._connection().execute(sql, params).fetchall()
        return [
            dict(row)
            for row in rows
            if self._session_matches_visible_continuation(
                row["session_id"], visible_session_id,
            )
        ]

    def _record_local_landing(self, conn, row) -> int:
        """Project one exact local landing while its push remains independent."""

        from .bestplan_proof import ProofLedger
        from .review_engine import ReviewStore

        try:
            record, _validated = decode_local_push_row(
                row, _validate_stored_plan_row,
            )
        except LocalPushStateError:
            return 0
        review_job = conn.execute(
            "SELECT * FROM review_jobs WHERE job_id=?",
            (record["review"]["job_id"],),
        ).fetchone()
        if review_job is None:
            return 0
        if review_job["state"] == "landing_prepared":
            if (
                review_job["prepared_consumer_plan_id"] != str(row["plan_id"])
                or review_job["prepared_target_digest"]
                != record["review"]["target_digest"]
                or review_job["prepared_review_receipt_digest"]
                != record["review"]["receipt_digest"]
                or review_job["integration_oid"] != record["integration_oid"]
                or review_job["check_receipt_digest"]
                != record["check_set_digest"]
                or not isinstance(review_job["owner_id"], str)
                or not review_job["owner_id"]
            ):
                return 0
            if conn.execute(
                "UPDATE review_jobs SET state='landed' "
                "WHERE job_id=? AND state='landing_prepared' "
                "AND cancel_requested=0 AND prepared_consumer_plan_id=? "
                "AND prepared_target_digest=? "
                "AND prepared_review_receipt_digest=?",
                (
                    record["review"]["job_id"],
                    str(row["plan_id"]),
                    record["review"]["target_digest"],
                    record["review"]["receipt_digest"],
                ),
            ).rowcount != 1:
                return 0
            ReviewStore._append_event_conn(
                conn,
                job_id=str(review_job["job_id"]),
                generation=int(review_job["current_generation"]),
                owner_id=str(review_job["owner_id"]),
                fencing_token=int(review_job["fencing_token"]),
                operation_id=f"landing-reconciled:{row['plan_id']}",
                kind="landing_reconciled",
                target_digest=str(record["review"]["target_digest"]),
                payload={
                    "consumer_plan_id": str(row["plan_id"]),
                    "observed_local_main": "integration",
                },
            )
        elif review_job["state"] != "landed":
            return 0

        now = time.time()
        ProofLedger(self).append_advisory_in_transaction(
            conn,
            plan_id=str(row["plan_id"]),
            kind="local_landing_recovered_advisory",
            raw_output={"status": "local_landing_recovered"},
            output_source="process",
            compatibility_dispatch_state="terminal",
            compatibility_clear_dispatch_owner=True,
        )
        return conn.execute(
            """UPDATE bestplan_plans
               SET local_push_state='awaiting', local_push_updated_at=?,
                   state=?
               WHERE plan_id=? AND local_push_state='prepared'
                 AND local_push_json=? AND state IN (?, ?)""",
            (
                now,
                PlanState.COMPLETED_LOCAL,
                row["plan_id"],
                row["local_push_json"],
                PlanState.RUNNING,
                PlanState.WAITING,
            ),
        ).rowcount

    def prepare_local_push(
        self,
        plan_id: str,
        *,
        session_id: str,
        profile: str,
        workspace: str,
        expected_target_oid: str,
        integration_oid: str,
        check_set_digest: str,
        review_target: Any,
        review_receipt_digest: str,
        target: Any,
        expires_at: int,
    ) -> Optional[dict[str, Any]]:
        """Durably bind one prompt before the checked local-main effect."""

        from .bestplan_local_git import LocalMainPushTarget
        from .review_engine import (
            ReviewStore,
            ReviewStoreConflict,
            ReviewTarget,
            ReviewValidationError,
        )

        now = time.time()
        if (
            not isinstance(target, LocalMainPushTarget)
            or not isinstance(review_target, ReviewTarget)
            or review_target.source_kind != "bestplan_integration"
            or not isinstance(review_receipt_digest, str)
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
            or expires_at <= now
            or expires_at - now > LOCAL_PUSH_MAX_TTL_SECONDS
        ):
            return None
        expected_workspace = _canonical_workspace(workspace)

        def prepare(conn):
            row = conn.execute(
                "SELECT * FROM bestplan_plans WHERE plan_id=?", (str(plan_id),),
            ).fetchone()
            if row is None:
                return None
            values = dict(row)
            try:
                validated = _validate_stored_plan_row(values)
            except BestplanError:
                return None
            contract = validated.contract
            snapshot = validated.source_snapshot
            if (
                validated.execution_protocol != 2
                or not isinstance(contract, Mapping)
                or contract.get("schema") != "hermes.bestplan.local-go.v1"
                or contract.get("version") != 1
                or contract.get("mode") != "local_main"
                or snapshot is None
                or values.get("state") not in {PlanState.RUNNING, PlanState.WAITING}
                or values.get("session_id") != str(session_id)
                or values.get("profile") != str(profile)
                or values.get("workspace") != expected_workspace
                or expected_target_oid != snapshot.head_oid
                or target.integration_oid != integration_oid
                or review_target.plan_id != str(plan_id)
                or review_target.base_oid != snapshot.head_oid
                or review_target.local_target_oid != snapshot.head_oid
                or review_target.integration_oid != integration_oid
                or review_target.check_receipt_digest != check_set_digest
                or review_target.approval_digest != values.get("approval_digest")
                or review_target.contract_digest
                != values.get("promotion_contract_digest")
                or values.get("current_phase") != "captured"
                or any(
                    values.get(name) is not None
                    for name in (
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
                        "verified_at",
                        "completed_at",
                    )
                )
            ):
                return None
            existing_raw = values.get("local_push_json")
            existing_state = values.get("local_push_state")
            if existing_raw is not None or existing_state is not None:
                try:
                    existing_record, _existing_plan = decode_local_push_row(
                        values, _validate_stored_plan_row,
                    )
                except LocalPushStateError:
                    return None
                if (
                    existing_state == "prepared"
                    and existing_record["expected_target_oid"]
                    == expected_target_oid
                    and existing_record["integration_oid"] == integration_oid
                    and existing_record["check_set_digest"] == check_set_digest
                    and existing_record["review"]["target_digest"]
                    == review_target.target_digest
                    and existing_record["review"]["receipt_digest"]
                    == review_receipt_digest
                ):
                    return {**existing_record, "state": "prepared"}
                return None
            if any(
                values.get(name) is not None
                for name in (
                    "review_job_id",
                    "review_target_digest",
                    "review_receipt_digest",
                )
            ):
                return None
            try:
                stored_pass = ReviewStore.latest_exact_pass_in_transaction(
                    conn,
                    target=review_target,
                    review_receipt_digest=review_receipt_digest,
                )
                bound_values = {
                    **values,
                    "review_job_id": stored_pass.job_id,
                    "review_target_digest": review_target.target_digest,
                    "review_receipt_digest": review_receipt_digest,
                }
                record = build_local_push_record(
                    row=bound_values,
                    plan=validated,
                    plan_id=str(plan_id),
                    session_id=str(session_id),
                    profile=str(profile),
                    workspace=expected_workspace,
                    expected_target_oid=expected_target_oid,
                    integration_oid=integration_oid,
                    check_set_digest=check_set_digest,
                    review_job_id=stored_pass.job_id,
                    review_target_digest=review_target.target_digest,
                    review_receipt_digest=review_receipt_digest,
                    target=target,
                    expires_at=expires_at,
                )
                raw = canonical_local_push_json(record)
            except (
                LocalPushStateError,
                ReviewStoreConflict,
                ReviewValidationError,
            ):
                return None
            ReviewStore.consume_latest_pass_in_transaction(
                conn,
                target=review_target,
                review_receipt_digest=review_receipt_digest,
                consumer_plan_id=str(plan_id),
            )
            changed = conn.execute(
                """UPDATE bestplan_plans
                   SET local_push_json=?, local_push_state='prepared',
                       local_push_updated_at=?, review_job_id=?,
                       review_target_digest=?, review_receipt_digest=?
                   WHERE plan_id=? AND local_push_json IS NULL
                     AND local_push_state IS NULL AND review_job_id IS NULL
                     AND review_target_digest IS NULL
                     AND review_receipt_digest IS NULL""",
                (
                    raw,
                    now,
                    stored_pass.job_id,
                    review_target.target_digest,
                    review_receipt_digest,
                    str(plan_id),
                ),
            ).rowcount
            if changed != 1:
                raise BestplanError("local push lost the review preparation race")
            return {**record, "state": "prepared"}

        try:
            return self._execute_write(prepare)
        except (BestplanError, ReviewStoreConflict, ReviewValidationError):
            return None

    def activate_local_push(
        self,
        plan_id: str,
        *,
        landing_receipt: Any,
    ) -> bool:
        """Expose a prompt only after the exact local landing postflight."""

        from .bestplan_local_git import LocalMainLandingReceipt

        if not isinstance(landing_receipt, LocalMainLandingReceipt):
            return False

        def activate(conn):
            row = conn.execute(
                "SELECT * FROM bestplan_plans WHERE plan_id=?", (str(plan_id),),
            ).fetchone()
            if row is None or row["local_push_state"] != "prepared":
                return 0
            try:
                record, _validated = decode_local_push_row(
                    row, _validate_stored_plan_row,
                )
            except LocalPushStateError:
                return 0
            if (
                landing_receipt.target_ref != record["local_ref"]
                or landing_receipt.old_oid != record["expected_target_oid"]
                or landing_receipt.new_oid != record["integration_oid"]
                or landing_receipt.check_receipt_digest
                != record["check_set_digest"]
                or not isinstance(landing_receipt.authorization_digest, str)
                or not landing_receipt.authorization_digest
            ):
                return 0
            review_job = conn.execute(
                "SELECT state, landing_authorization_digest FROM review_jobs "
                "WHERE job_id=?",
                (record["review"]["job_id"],),
            ).fetchone()
            if (
                review_job is None
                or review_job["state"] != "landing_claimed"
                or review_job["landing_authorization_digest"]
                != landing_receipt.authorization_digest
            ):
                return 0
            if conn.execute(
                "UPDATE review_jobs SET state='landed' "
                "WHERE job_id=? AND state='landing_claimed' "
                "AND landing_authorization_digest=?",
                (
                    record["review"]["job_id"],
                    landing_receipt.authorization_digest,
                ),
            ).rowcount != 1:
                return 0
            if self._record_local_landing(conn, row) != 1:
                raise BestplanError("local landing activation lost its plan state")
            return 1

        try:
            return bool(self._execute_write(activate))
        except BestplanError:
            return False

    def claim_landing(
        self,
        plan_id: str,
        *,
        owner_id: str,
        fencing_token: int,
        owner_pid: int,
        owner_process_start_id: str,
        operation_id: str,
    ):
        """Serialize cancellation with one process-bound local Git claim."""

        from .review_engine import (
            ReviewLeaseConflict,
            ReviewStore,
            ReviewValidationError,
            _issue_landing_authorization,
        )

        if (
            not isinstance(owner_id, str)
            or not owner_id
            or isinstance(fencing_token, bool)
            or not isinstance(fencing_token, int)
            or fencing_token < 0
            or isinstance(owner_pid, bool)
            or not isinstance(owner_pid, int)
            or owner_pid < 1
            or not isinstance(owner_process_start_id, str)
            or not owner_process_start_id
            or not isinstance(operation_id, str)
            or not operation_id
        ):
            raise ReviewValidationError("landing claim identity is invalid")
        lock_handle = None

        def claim(conn):
            nonlocal lock_handle
            row = conn.execute(
                "SELECT * FROM bestplan_plans WHERE plan_id=?", (str(plan_id),),
            ).fetchone()
            if row is None or row["local_push_state"] != "prepared":
                raise ReviewLeaseConflict("landing is not prepared")
            try:
                record, validated = decode_local_push_row(
                    row, _validate_stored_plan_row,
                )
            except LocalPushStateError as exc:
                raise ReviewLeaseConflict("landing preparation is stale") from exc
            review_job = conn.execute(
                "SELECT * FROM review_jobs WHERE job_id=?",
                (record["review"]["job_id"],),
            ).fetchone()
            if review_job is None:
                raise ReviewLeaseConflict("landing review job is missing")
            if review_job["state"] == "landing_claimed":
                raise ReviewLeaseConflict("landing_already_claimed")
            if bool(review_job["cancel_requested"]):
                raise ReviewLeaseConflict("landing review is cancelled")
            claim_now_ns = time.time_ns()
            lease_expires_at_ns = review_job["lease_expires_at_ns"]
            if (
                lease_expires_at_ns is None
                or claim_now_ns > int(lease_expires_at_ns)
            ):
                raise ReviewLeaseConflict("landing review owner lease has expired")
            if (
                review_job["state"] != "landing_prepared"
                or review_job["owner_id"] != owner_id
                or int(review_job["fencing_token"]) != fencing_token
                or review_job["prepared_consumer_plan_id"] != str(plan_id)
                or review_job["prepared_target_digest"]
                != record["review"]["target_digest"]
                or review_job["prepared_review_receipt_digest"]
                != record["review"]["receipt_digest"]
                or review_job["integration_oid"] != record["integration_oid"]
                or review_job["check_receipt_digest"]
                != record["check_set_digest"]
            ):
                raise ReviewLeaseConflict("landing review fencing token differs")
            try:
                lock_path, lock_handle = _open_repository_effect_lock(
                    validated.source_snapshot.repo
                )
                fcntl.flock(
                    lock_handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BaseException:
                if lock_handle is not None:
                    lock_handle.close()
                lock_handle = None
                raise ReviewLeaseConflict("repository landing effect is active") from None
            values = {
                "plan_id": str(plan_id),
                "review_job_id": str(review_job["job_id"]),
                "target_digest": str(record["review"]["target_digest"]),
                "integration_oid": str(record["integration_oid"]),
                "check_receipt_digest": str(record["check_set_digest"]),
                "fencing_token": fencing_token,
                "owner_pid": owner_pid,
                "owner_process_start_id": owner_process_start_id,
                "repository_id": validated.source_snapshot.repo.repository_id,
                "repository_effect_lock_path": lock_path,
            }
            authorization = _issue_landing_authorization(
                lock_handle=lock_handle, **values,
            )
            changed = conn.execute(
                """
                UPDATE review_jobs
                SET state='landing_claimed', landing_owner_pid=?,
                    landing_owner_process_start_id=?,
                    landing_repository_effect_lock_path=?,
                    landing_authorization_digest=?, landing_operation_active=1
                WHERE job_id=? AND state='landing_prepared'
                  AND cancel_requested=0 AND owner_id=? AND fencing_token=?
                  AND lease_expires_at_ns IS NOT NULL
                  AND lease_expires_at_ns>=?
                """,
                (
                    owner_pid,
                    owner_process_start_id,
                    lock_path,
                    authorization.authorization_digest,
                    review_job["job_id"],
                    owner_id,
                    fencing_token,
                    claim_now_ns,
                ),
            ).rowcount
            if changed != 1:
                raise ReviewLeaseConflict("landing claim lost its compare-and-swap")
            ReviewStore._append_event_conn(
                conn,
                job_id=str(review_job["job_id"]),
                generation=int(review_job["current_generation"]),
                owner_id=owner_id,
                fencing_token=fencing_token,
                operation_id=operation_id,
                kind="landing_claimed",
                target_digest=str(record["review"]["target_digest"]),
                payload={
                    "owner_pid": owner_pid,
                    "owner_process_start_id": owner_process_start_id,
                },
            )
            return authorization

        try:
            return self._execute_write(claim)
        except BaseException:
            if lock_handle is not None:
                lock_handle.close()
            raise

    def recover_landing_claim(
        self,
        plan_id: str,
        *,
        owner_is_live: Callable[[int, str], bool],
        observe_local_main: Callable[..., str],
        now_ns: int,
    ) -> LandingRecoveryResult:
        """Observe a dead claimed effect under its repository lock."""

        if (
            not callable(owner_is_live)
            or not callable(observe_local_main)
            or isinstance(now_ns, bool)
            or not isinstance(now_ns, int)
            or now_ns < 0
        ):
            return LandingRecoveryResult("drifted")
        with self._read_lock():
            row = self._connection().execute(
                "SELECT * FROM bestplan_plans WHERE plan_id=?", (str(plan_id),),
            ).fetchone()
            if row is None:
                return LandingRecoveryResult("drifted")
            try:
                record, validated = decode_local_push_row(
                    row, _validate_stored_plan_row,
                )
            except LocalPushStateError:
                return LandingRecoveryResult("drifted")
            review_job = self._connection().execute(
                "SELECT * FROM review_jobs WHERE job_id=?",
                (record["review"]["job_id"],),
            ).fetchone()
        if review_job is None or review_job["state"] != "landing_claimed":
            return LandingRecoveryResult("drifted")
        pid = review_job["landing_owner_pid"]
        start_id = review_job["landing_owner_process_start_id"]
        lock_path = review_job["landing_repository_effect_lock_path"]
        if (
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid < 1
            or not isinstance(start_id, str)
            or not start_id
            or not isinstance(lock_path, str)
            or not lock_path
            or lock_path != validated.source_snapshot.repo.common_dir
        ):
            return LandingRecoveryResult("drifted")
        # The process can outlive its exact Git child.  Only the repository
        # effect lock proves that the mutating operation is still active; a
        # live persisted PID must not prevent same-process reconciliation once
        # that exact lock is free.
        try:
            _verified_path, lock_handle = _open_repository_effect_lock(
                validated.source_snapshot.repo
            )
        except (BestplanError, OSError, ValueError):
            return LandingRecoveryResult("drifted")
        try:
            fcntl.flock(
                lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except (OSError, ValueError):
            lock_handle.close()
            return LandingRecoveryResult("owner_alive")
        try:
            with self._read_lock():
                current = self._connection().execute(
                    "SELECT state, landing_owner_pid, "
                    "landing_owner_process_start_id, "
                    "landing_authorization_digest FROM review_jobs "
                    "WHERE job_id=?",
                    (record["review"]["job_id"],),
                ).fetchone()
            if (
                current is None
                or current["state"] != "landing_claimed"
                or current["landing_owner_pid"] != pid
                or current["landing_owner_process_start_id"] != start_id
                or current["landing_authorization_digest"]
                != review_job["landing_authorization_digest"]
            ):
                return LandingRecoveryResult("drifted")
            try:
                observed = observe_local_main(
                    snapshot=validated.source_snapshot,
                    expected_target_oid=record["expected_target_oid"],
                    integration_oid=record["integration_oid"],
                    deadline=time.monotonic() + 10.0,
                )
            except Exception:
                return LandingRecoveryResult("observation_unavailable")
            if observed == "integration":
                def land_recovered(conn):
                    job_changed = conn.execute(
                        "UPDATE review_jobs SET state='landed' "
                        "WHERE job_id=? AND state='landing_claimed' "
                        "AND landing_authorization_digest=?",
                        (
                            record["review"]["job_id"],
                            review_job["landing_authorization_digest"],
                        ),
                    ).rowcount
                    stored_row = conn.execute(
                        "SELECT * FROM bestplan_plans WHERE plan_id=?",
                        (str(plan_id),),
                    ).fetchone()
                    if job_changed != 1 or stored_row is None:
                        return 0
                    return self._record_local_landing(conn, stored_row)

                if self._execute_write(land_recovered) == 1:
                    return LandingRecoveryResult("landed")
                return LandingRecoveryResult("drifted")
            if observed == "expected":
                from .review_engine import ReviewStore

                def release_pre_effect(conn):
                    current_job = conn.execute(
                        "SELECT * FROM review_jobs WHERE job_id=?",
                        (record["review"]["job_id"],),
                    ).fetchone()
                    if (
                        current_job is None
                        or current_job["state"] != "landing_claimed"
                        or current_job["landing_owner_pid"] != pid
                        or current_job["landing_owner_process_start_id"] != start_id
                        or current_job["landing_authorization_digest"]
                        != review_job["landing_authorization_digest"]
                    ):
                        return 0
                    ReviewStore._append_event_conn(
                        conn,
                        job_id=str(current_job["job_id"]),
                        generation=int(current_job["current_generation"]),
                        owner_id=str(current_job["owner_id"]),
                        fencing_token=int(current_job["fencing_token"]),
                        operation_id=(
                            f"landing-claim-released:{plan_id}:"
                            f"{current_job['fencing_token']}"
                        ),
                        kind="landing_claim_released",
                        target_digest=str(current_job["target_digest"]),
                        payload={"observed_local_main": "expected"},
                    )
                    return conn.execute(
                        """
                        UPDATE review_jobs
                        SET state='landing_prepared', owner_id=NULL,
                            lease_expires_at_ns=NULL, landing_owner_pid=NULL,
                            landing_owner_process_start_id=NULL,
                            landing_repository_effect_lock_path=NULL,
                            landing_authorization_digest=NULL,
                            landing_operation_active=0
                        WHERE job_id=? AND state='landing_claimed'
                          AND landing_owner_pid=?
                          AND landing_owner_process_start_id=?
                          AND landing_authorization_digest=?
                        """,
                        (
                            record["review"]["job_id"],
                            pid,
                            start_id,
                            review_job["landing_authorization_digest"],
                        ),
                    ).rowcount

                if self._execute_write(release_pre_effect) == 1:
                    return LandingRecoveryResult("retry_pre_effect")
            return LandingRecoveryResult("drifted")
        finally:
            lock_handle.close()

    def mark_landing_observation_pending(
        self,
        plan_id: str,
        *,
        authorization: Any,
    ) -> bool:
        """Mark a finished effect operation for read-only reconciliation."""

        validate = getattr(authorization, "validate_digest", None)
        if not callable(validate) or not bool(validate()):
            return False

        def mark(conn):
            row = conn.execute(
                "SELECT review_job_id FROM bestplan_plans WHERE plan_id=?",
                (str(plan_id),),
            ).fetchone()
            if row is None or row["review_job_id"] != authorization.review_job_id:
                return 0
            return conn.execute(
                """
                UPDATE review_jobs SET landing_operation_active=0
                WHERE job_id=? AND state='landing_claimed'
                  AND fencing_token=? AND landing_owner_pid=?
                  AND landing_owner_process_start_id=?
                  AND landing_authorization_digest=?
                """,
                (
                    authorization.review_job_id,
                    authorization.fencing_token,
                    authorization.owner_pid,
                    authorization.owner_process_start_id,
                    authorization.authorization_digest,
                ),
            ).rowcount

        changed = bool(self._execute_write(mark))
        if changed:
            authorization.release_effect_lock()
        return changed

    def _set_local_push_state(
        self,
        plan_id: str,
        *,
        expected_state: str,
        new_state: str,
        expected_json: str | None = None,
    ) -> bool:
        if expected_state not in LOCAL_PUSH_STATES or new_state not in LOCAL_PUSH_STATES:
            return False

        def transition(conn):
            if expected_state == "prepared" and new_state == "awaiting":
                row = conn.execute(
                    "SELECT * FROM bestplan_plans WHERE plan_id=?",
                    (str(plan_id),),
                ).fetchone()
                if (
                    row is None
                    or row["local_push_state"] != "prepared"
                    or expected_json is not None
                    and row["local_push_json"] != expected_json
                ):
                    return 0
                try:
                    _record, _validated = decode_local_push_row(
                        row, _validate_stored_plan_row,
                    )
                except LocalPushStateError:
                    return 0
                return self._record_local_landing(conn, row)
            if expected_state == "prepared" and new_state in {
                "expired", "not_landed", "stale",
            }:
                from .bestplan_proof import ProofLedger

                now = time.time()
                row = conn.execute(
                    "SELECT * FROM bestplan_plans WHERE plan_id=?",
                    (str(plan_id),),
                ).fetchone()
                if (
                    row is None
                    or row["local_push_state"] != "prepared"
                    or row["state"] not in {PlanState.RUNNING, PlanState.WAITING}
                    or expected_json is not None
                    and row["local_push_json"] != expected_json
                ):
                    return 0
                ProofLedger(self).append_advisory_in_transaction(
                    conn,
                    plan_id=str(plan_id),
                    kind="local_execution_reconciled_failed_advisory",
                    raw_output={
                        "status": "local_execution_failed",
                        "local_push_state": new_state,
                    },
                    output_source="process",
                    compatibility_error="dispatch_failed",
                    compatibility_dispatch_state="terminal",
                    compatibility_clear_dispatch_owner=True,
                )
                return conn.execute(
                    """UPDATE bestplan_plans
                       SET local_push_state=?, local_push_updated_at=?, state=?
                       WHERE plan_id=? AND local_push_state='prepared'
                         AND state IN (?, ?)
                         AND (? IS NULL OR local_push_json=?)""",
                    (
                        new_state,
                        now,
                        PlanState.FAILED,
                        str(plan_id),
                        PlanState.RUNNING,
                        PlanState.WAITING,
                        expected_json,
                        expected_json,
                    ),
                ).rowcount
            sql = (
                "UPDATE bestplan_plans SET local_push_state=?, "
                "local_push_updated_at=? WHERE plan_id=? AND local_push_state=?"
            )
            params: list[Any] = [
                new_state, time.time(), str(plan_id), expected_state,
            ]
            if expected_json is not None:
                sql += " AND local_push_json=?"
                params.append(expected_json)
            return conn.execute(sql, params).rowcount

        return bool(self._execute_write(transition))

    def claim_local_push(
        self,
        plan_id: str,
        *,
        now: float | None = None,
    ) -> Optional[dict[str, Any]]:
        """Atomically claim one awaiting or exact-recovery push effect."""

        observed_now = time.time() if now is None else float(now)
        if not math.isfinite(observed_now):
            return None

        def claim(conn):
            row = conn.execute(
                "SELECT * FROM bestplan_plans WHERE plan_id=?", (str(plan_id),),
            ).fetchone()
            if row is None or row["local_push_state"] not in {
                "awaiting", "effect_unknown",
            }:
                return None
            try:
                record, _validated = decode_local_push_row(
                    row, _validate_stored_plan_row,
                )
            except LocalPushStateError:
                conn.execute(
                    "UPDATE bestplan_plans SET local_push_state='stale', "
                    "local_push_updated_at=? WHERE plan_id=? AND local_push_state=?",
                    (time.time(), str(plan_id), row["local_push_state"]),
                )
                return None
            if observed_now >= record["expires_at"]:
                conn.execute(
                    "UPDATE bestplan_plans SET local_push_state='expired', "
                    "local_push_updated_at=? WHERE plan_id=? AND local_push_state=? "
                    "AND local_push_json=?",
                    (
                        time.time(), str(plan_id), row["local_push_state"],
                        row["local_push_json"],
                    ),
                )
                return None
            previous = row["local_push_state"]
            changed = conn.execute(
                """UPDATE bestplan_plans
                   SET local_push_state='pushing', local_push_updated_at=?
                   WHERE plan_id=? AND local_push_state=?
                     AND local_push_json=?""",
                (
                    time.time(), str(plan_id), previous,
                    row["local_push_json"],
                ),
            ).rowcount
            return {**record, "state": "pushing"} if changed == 1 else None

        return self._execute_write(claim)

    def list_active_local_pushes(self, session_id: str) -> list[dict[str, Any]]:
        visible_session_id = str(session_id)
        placeholders = ",".join("?" for _ in LOCAL_PUSH_ACTIVE_STATES)
        params: list[Any] = [
            *LOCAL_PUSH_ACTIVE_STATES,
            visible_session_id,
        ]
        with self._read_lock():
            rows = self._connection().execute(
                "SELECT * FROM bestplan_plans "
                f"WHERE local_push_state IN ({placeholders}) "
                "AND (session_id=? OR (execution_protocol=2 "
                "AND promotion_contract_version=1 "
                "AND promotion_mode='local_main')) "
                "ORDER BY created_at DESC",
                params,
            ).fetchall()
        return [
            dict(row)
            for row in rows
            if self._session_matches_visible_continuation(
                row["session_id"], visible_session_id,
            )
        ]

    def reconcile_local_pushes(
        self,
        *,
        classify_local_main: Optional[Callable[..., str]] = None,
        classify_remote: Optional[Callable[..., str]] = None,
        now: float | None = None,
    ) -> int:
        """Resolve crash-retained prompt states from exact Git read-back."""

        from .bestplan_local_push import reconcile_local_pushes

        return reconcile_local_pushes(
            self,
            classify_local_main=classify_local_main,
            classify_remote=classify_remote,
            now=now,
        )

    def approve_plan(self, plan_id: str, approver: str = "user") -> bool:
        def approve(conn):
            row = conn.execute(
                "SELECT * FROM bestplan_plans WHERE plan_id = ? AND state = ?",
                (plan_id, PlanState.PENDING),
            ).fetchone()
            if row is None:
                return 0
            try:
                validated = _validate_stored_plan_row(
                    row, allow_v1_null_approval=True,
                )
            except BestplanError:
                return 0
            return conn.execute(
                "UPDATE bestplan_plans SET state=?, approved_at=?, approved_by=?, "
                "approval_digest=? WHERE plan_id=? AND state=?",
                (
                    PlanState.APPROVED,
                    time.time(),
                    approver,
                    validated.approval_digest,
                    plan_id,
                    PlanState.PENDING,
                ),
            ).rowcount
        return bool(self._execute_write(approve))

    def reject_plan(self, plan_id: str) -> bool:
        return bool(self._execute_write(lambda conn: conn.execute(
            "UPDATE bestplan_plans SET state=? WHERE plan_id=? AND state IN (?, ?)",
            (
                PlanState.REJECTED,
                plan_id,
                PlanState.PENDING,
                PlanState.PROVISIONAL,
            ),
        ).rowcount))

    @staticmethod
    def _supersede_unstarted_rows(
        conn: sqlite3.Connection,
        *,
        session_id: str,
        profile: str,
        workspace: str,
        baseline_fingerprint: str,
        before: float,
        replacement_plan_id: str | None,
        include_provisional: bool,
        include_compression_lineage: bool,
    ) -> int:
        """Apply one ordered supersession policy inside the caller transaction."""
        states = [PlanState.PENDING, PlanState.APPROVED]
        if include_provisional:
            states.insert(0, PlanState.PROVISIONAL)
        state_placeholders = ", ".join("?" for _ in states)

        direct_clauses = [
            "session_id=?",
            "profile=?",
            "workspace=?",
            "created_at<?",
        ]
        direct_params: list[Any] = [session_id, profile, workspace, before]
        direct_clauses.append("baseline_fingerprint=?")
        direct_params.append(baseline_fingerprint)
        if replacement_plan_id is not None:
            direct_clauses.append("plan_id!=?")
            direct_params.append(replacement_plan_id)
        direct_clauses.append(f"state IN ({state_placeholders})")
        direct_params.extend(states)
        changed = conn.execute(
            "UPDATE bestplan_plans SET state=? WHERE "
            + " AND ".join(direct_clauses),
            (PlanState.REJECTED, *direct_params),
        ).rowcount

        has_sessions = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
        ).fetchone()
        if not include_compression_lineage or has_sessions is None:
            return changed

        lineage_clauses = [
            "session_id IN (SELECT id FROM compression_lineage)",
            "profile=?",
            "workspace=?",
            "execution_protocol=2",
            "promotion_contract_version=1",
            "promotion_mode='local_main'",
            "created_at<?",
        ]
        lineage_params: list[Any] = [profile, workspace, before]
        lineage_clauses.append("baseline_fingerprint=?")
        lineage_params.append(baseline_fingerprint)
        if replacement_plan_id is not None:
            lineage_clauses.append("plan_id!=?")
            lineage_params.append(replacement_plan_id)
        lineage_clauses.append(f"state IN ({state_placeholders})")
        lineage_params.extend(states)
        conn.execute(
            """WITH RECURSIVE compression_lineage(id) AS (
                   SELECT ?
                   UNION
                   SELECT parent.id
                   FROM compression_lineage AS lineage
                   JOIN sessions AS child ON child.id=lineage.id
                   JOIN sessions AS parent ON parent.id=child.parent_session_id
                   WHERE parent.end_reason='compression'
                     AND json_extract(
                           COALESCE(child.model_config, '{}'), '$._branched_from'
                         ) IS NULL
                     AND json_extract(
                           COALESCE(child.model_config, '{}'), '$._delegate_from'
                         ) IS NULL
                     AND COALESCE(child.source, '')!='tool'
               )
               UPDATE bestplan_plans SET state=? WHERE """
            + " AND ".join(lineage_clauses),
            (session_id, PlanState.REJECTED, *lineage_params),
        )
        changed += int(conn.execute("SELECT changes()").fetchone()[0])
        return changed

    def supersede_unstarted_plans(
        self,
        *,
        session_id: str,
        profile: str,
        workspace: str,
        baseline_fingerprint: str,
        before: float,
        local_execution: bool = False,
    ) -> int:
        """Reject older unstarted plans after a durable non-plan outcome."""
        if type(local_execution) is not bool:
            raise BestplanError("local_execution must be true or false")
        expected_session = str(session_id)
        expected_profile = str(profile)
        expected_workspace = _canonical_workspace(workspace)
        if baseline_fingerprint is None:
            raise BestplanError("baseline_fingerprint must be non-empty")
        expected_baseline = str(baseline_fingerprint).strip()
        if not expected_baseline:
            raise BestplanError("baseline_fingerprint must be non-empty")
        cutoff = float(before)
        if not math.isfinite(cutoff):
            raise BestplanError("supersession cutoff must be finite")

        def supersede(conn):
            return self._supersede_unstarted_rows(
                conn,
                session_id=expected_session,
                profile=expected_profile,
                workspace=expected_workspace,
                baseline_fingerprint=expected_baseline,
                before=cutoff,
                replacement_plan_id=None,
                include_provisional=True,
                include_compression_lineage=local_execution,
            )

        return int(self._execute_write(supersede))

    def commit_provisional_plan(self, plan_id: str) -> bool:
        """Expose one captured plan only after its transcript is durable."""
        def commit(conn):
            row = conn.execute(
                "SELECT * FROM bestplan_plans WHERE plan_id=? AND state=?",
                (plan_id, PlanState.PROVISIONAL),
            ).fetchone()
            if row is None:
                return 0
            try:
                _validate_stored_plan_row(row)
            except BestplanError:
                return 0
            changed = conn.execute(
                "UPDATE bestplan_plans SET state=? WHERE plan_id=? AND state=?",
                (PlanState.PENDING, plan_id, PlanState.PROVISIONAL),
            ).rowcount
            if changed != 1:
                return 0
            is_local_go = (
                int(row["execution_protocol"] or 1) == 2
                and int(row["promotion_contract_version"] or 0) == 1
                and row["promotion_mode"] == "local_main"
            )
            self._supersede_unstarted_rows(
                conn,
                session_id=row["session_id"],
                profile=row["profile"],
                workspace=row["workspace"],
                baseline_fingerprint=row["baseline_fingerprint"],
                before=float(row["created_at"]),
                replacement_plan_id=plan_id,
                include_provisional=False,
                include_compression_lineage=is_local_go,
            )
            return changed

        return bool(self._execute_write(commit))

    def list_approved_matching(
        self, *, session_id: str, workspace: str,
        baseline_fingerprint: Optional[str] = None, profile: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        expected_profile = _active_profile() if profile is None else str(profile)
        expected_workspace = _canonical_workspace(workspace)
        expected_baseline = baseline_fingerprint or compute_baseline_fingerprint(workspace)
        result = []
        for row in self.list_for_session(session_id):
            if not (
                row["state"] == PlanState.APPROVED
                and row["profile"] == expected_profile
                and row["workspace"] == expected_workspace
                and row["baseline_fingerprint"] == expected_baseline
            ):
                continue
            try:
                _validate_stored_plan_row(row)
            except BestplanError:
                continue
            result.append(row)
        return result

    def atomic_claim_approved(
        self,
        plan_id: str,
        baseline_fingerprint: str,
        *,
        session_id: Optional[str] = None,
        profile: Optional[str] = None,
        workspace: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Approve (when pending), revalidate, and claim in one transaction."""
        def claim(conn):
            row = conn.execute(
                "SELECT * FROM bestplan_plans WHERE plan_id = ?", (plan_id,),
            ).fetchone()
            if row is None or row["state"] not in (PlanState.PENDING, PlanState.APPROVED):
                return None
            if int(row["execution_protocol"] or 1) != 1:
                return None
            if session_id is not None and row["session_id"] != str(session_id):
                return None
            if profile is not None and row["profile"] != str(profile):
                return None
            if workspace is not None and row["workspace"] != _canonical_workspace(workspace):
                return None
            if row["baseline_fingerprint"] != baseline_fingerprint:
                return None
            try:
                validated = _validate_stored_plan_row(row)
            except BestplanError:
                return None
            now = time.time()
            changed = conn.execute(
                """UPDATE bestplan_plans SET state=?, approved_at=COALESCE(approved_at, ?),
                   approved_by=COALESCE(approved_by, 'user:bare-go'), started_at=?
                   WHERE plan_id=? AND state IN (?, ?) AND approval_digest=?""",
                (
                    PlanState.RUNNING, now, now, plan_id,
                    PlanState.PENDING, PlanState.APPROVED,
                    validated.approval_digest,
                ),
            ).rowcount
            if changed != 1:
                return None
            return dict(conn.execute(
                "SELECT * FROM bestplan_plans WHERE plan_id = ?", (plan_id,),
            ).fetchone())
        return self._execute_write(claim)

    def prepare_dispatch_intent(
        self,
        plan_id: str,
        baseline_fingerprint: str,
        *,
        resolved_runtimes: list[dict[str, Any]],
        session_id: str,
        profile: str,
        workspace: str,
    ) -> Optional[dict[str, Any]]:
        """Atomically approve and persist the deterministic dispatch outbox."""
        dispatch_id = f"bestplan-{plan_id}"

        def prepare(conn):
            row = conn.execute(
                "SELECT * FROM bestplan_plans WHERE plan_id=?", (plan_id,),
            ).fetchone()
            if row is None:
                return None
            try:
                validated = _validate_stored_plan_row(row)
            except BestplanError:
                return None
            protocol = int(row["execution_protocol"] or 1)
            try:
                safe_runtimes = sanitize_runtime_metadata(
                    resolved_runtimes,
                    execution_protocol=protocol,
                )
                runtime_json = json.dumps(
                    safe_runtimes,
                    ensure_ascii=True,
                    allow_nan=protocol != 2,
                    sort_keys=True,
                    separators=(",", ":") if protocol == 2 else None,
                )
            except (BestplanError, TypeError, ValueError):
                return None
            if row["state"] == PlanState.RUNNING and row["dispatch_state"] in {
                "intent", "unknown", "dispatching",
            }:
                return dict(row)
            if row["state"] not in (PlanState.PENDING, PlanState.APPROVED):
                return None
            if (
                row["session_id"] != str(session_id)
                or row["profile"] != str(profile)
                or row["workspace"] != _canonical_workspace(workspace)
                or row["baseline_fingerprint"] != baseline_fingerprint
            ):
                return None
            try:
                plan = validated.plan
                _v1_plan_constraints(plan, workspace=workspace)
            except Exception:
                return None
            now = time.time()
            changed = conn.execute(
                """UPDATE bestplan_plans SET state=?, approved_at=COALESCE(approved_at, ?),
                   approved_by=COALESCE(approved_by, 'user:bare-go'), started_at=?,
                   dispatch_id=?, dispatch_state='intent', resolved_runtime_json=?,
                   delegation_ids_json=?, dispatch_updated_at=?, error=NULL
                   WHERE plan_id=? AND state IN (?, ?)""",
                (
                    PlanState.RUNNING, now, now, dispatch_id,
                    runtime_json,
                    json.dumps([dispatch_id]), now, plan_id,
                    PlanState.PENDING, PlanState.APPROVED,
                ),
            ).rowcount
            if changed != 1:
                return None
            return dict(conn.execute(
                "SELECT * FROM bestplan_plans WHERE plan_id=?", (plan_id,),
            ).fetchone())

        return self._execute_write(prepare)

    def begin_dispatch_attempt(self, plan_id: str) -> bool:
        now = time.time()
        def begin(conn):
            row = conn.execute(
                "SELECT * FROM bestplan_plans WHERE plan_id=? AND state=?",
                (plan_id, PlanState.RUNNING),
            ).fetchone()
            if row is None:
                return 0
            try:
                _validate_stored_plan_row(row)
            except BestplanError:
                return 0
            return conn.execute(
                """UPDATE bestplan_plans SET dispatch_state='dispatching',
                   dispatch_owner=?, dispatch_started_at=?, dispatch_updated_at=?
                   WHERE plan_id=? AND state=?
                   AND dispatch_state IN ('intent', 'unknown')""",
                (f"pid:{os.getpid()}", now, now, plan_id, PlanState.RUNNING),
            ).rowcount

        return bool(self._execute_write(begin))

    def record_dispatch_unknown(self, plan_id: str, error: str) -> bool:
        def record(conn):
            row = conn.execute(
                "SELECT * FROM bestplan_plans WHERE plan_id=? AND state IN (?, ?)",
                (plan_id, PlanState.RUNNING, PlanState.WAITING),
            ).fetchone()
            if row is None:
                return 0
            if int(row["execution_protocol"] or 1) == 2:
                from .bestplan_proof import ProofLedger

                ProofLedger(self).append_advisory_in_transaction(
                    conn,
                    plan_id=plan_id,
                    kind="dispatch_unknown_advisory",
                    raw_output={"status": "dispatch_unknown", "detail": error},
                    output_source="model-broker",
                    compatibility_error="dispatch_unknown",
                    compatibility_dispatch_state="unknown",
                )
                return 1
            return conn.execute(
                """UPDATE bestplan_plans SET dispatch_state='unknown', error=?,
                   dispatch_updated_at=? WHERE plan_id=? AND state=?
                   AND dispatch_state='dispatching'""",
                (str(error), time.time(), plan_id, PlanState.RUNNING),
            ).rowcount

        return bool(self._execute_write(record))

    def recover_dead_dispatch_owners(self) -> int:
        def recover(conn):
            rows = conn.execute(
                "SELECT plan_id, dispatch_owner, execution_protocol FROM bestplan_plans "
                "WHERE state=? AND dispatch_state='dispatching'",
                (PlanState.RUNNING,),
            ).fetchall()
            changed = 0
            for row in rows:
                owner = str(row["dispatch_owner"] or "")
                if not owner.startswith("pid:"):
                    continue
                try:
                    pid = int(owner.split(":", 1)[1])
                    os.kill(pid, 0)
                    live = True
                except ProcessLookupError:
                    live = False
                except (PermissionError, ValueError):
                    live = True
                if live:
                    continue
                if int(row["execution_protocol"] or 1) == 2:
                    from .bestplan_proof import ProofLedger

                    ProofLedger(self).append_advisory_in_transaction(
                        conn,
                        plan_id=row["plan_id"],
                        kind="dispatch_owner_recovered_advisory",
                        raw_output={
                            "status": "recovered_dead_dispatch_owner",
                            "dispatch_owner": owner,
                        },
                        output_source="process",
                        compatibility_error="recovered_dead_dispatch_owner",
                        compatibility_dispatch_state="unknown",
                        compatibility_clear_dispatch_owner=True,
                    )
                    changed += 1
                    continue
                changed += conn.execute(
                    """UPDATE bestplan_plans SET dispatch_state='unknown',
                       dispatch_updated_at=?, error='recovered_dead_dispatch_owner'
                       WHERE plan_id=? AND dispatch_state='dispatching'
                       AND dispatch_owner=?""",
                    (time.time(), row["plan_id"], owner),
                ).rowcount
            return changed

        return int(self._execute_write(recover))

    def record_dispatch(
        self,
        plan_id: str,
        *,
        delegation_ids: list[str],
        sandbox_workspace: str = "",
    ) -> bool:
        def record(conn):
            row = conn.execute(
                "SELECT * FROM bestplan_plans WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            if row is not None and row["state"] == PlanState.COMPLETED_LOCAL:
                try:
                    validated = _validate_stored_plan_row(row)
                    decode_local_push_row(row, _validate_stored_plan_row)
                except (BestplanError, LocalPushStateError):
                    return 0
                return int(
                    isinstance(validated.contract, Mapping)
                    and validated.contract.get("schema")
                    == "hermes.bestplan.local-go.v1"
                )
            if row is None or row["state"] not in {
                PlanState.RUNNING, PlanState.COMPLETED_UNVERIFIED,
            }:
                return 0
            if int(row["execution_protocol"] or 1) == 2:
                from .bestplan_proof import ProofLedger

                safe_dispatch_id = str(
                    row["dispatch_id"] or f"bestplan-{plan_id}"
                )
                compatibility = (
                    {
                        "compatibility_dispatch_state": "scheduled",
                        "compatibility_delegation_ids_json": json.dumps(
                            [safe_dispatch_id]
                        ),
                        "compatibility_sandbox_workspace": "",
                    }
                    if (
                        row["current_phase"] == "captured"
                        and row["dispatch_state"] == "dispatching"
                        and isinstance(row["dispatch_owner"], str)
                        and bool(row["dispatch_owner"])
                    )
                    else {}
                )
                ProofLedger(self).append_advisory_in_transaction(
                    conn,
                    plan_id=plan_id,
                    kind="dispatch_scheduled_advisory",
                    raw_output={
                        "delegation_ids": delegation_ids,
                        "sandbox_workspace": str(sandbox_workspace or ""),
                    },
                    output_source="model-broker",
                    **compatibility,
                )
                return 1
            terminal = row["state"] == PlanState.COMPLETED_UNVERIFIED
            return conn.execute(
                """UPDATE bestplan_plans SET state=?, delegation_ids_json=?,
                   dispatch_state=?, dispatch_updated_at=?, error=NULL,
                   sandbox_workspace=? WHERE plan_id=?""",
                (
                    PlanState.COMPLETED_UNVERIFIED if terminal else PlanState.WAITING,
                    json.dumps(delegation_ids),
                    "terminal" if terminal else "scheduled",
                    time.time(), str(sandbox_workspace or ""), plan_id,
                ),
            ).rowcount
        return bool(self._execute_write(record))

    def record_dispatch_failure(self, plan_id: str, error: str) -> bool:
        def record(conn):
            row = conn.execute(
                "SELECT * FROM bestplan_plans WHERE plan_id=? AND state IN (?, ?)",
                (plan_id, PlanState.RUNNING, PlanState.WAITING),
            ).fetchone()
            if row is None:
                return 0
            if int(row["execution_protocol"] or 1) == 2:
                from .bestplan_proof import ProofLedger

                ProofLedger(self).append_advisory_in_transaction(
                    conn,
                    plan_id=plan_id,
                    kind="dispatch_failed_advisory",
                    raw_output={"status": "dispatch_failed", "detail": error},
                    output_source="model-broker",
                    compatibility_error="dispatch_failed",
                    compatibility_dispatch_state="terminal",
                )
                return 1
            return conn.execute(
                "UPDATE bestplan_plans SET state=?, error=?, completed_at=? "
                "WHERE plan_id=? AND state=?",
                (
                    PlanState.FAILED,
                    str(error),
                    time.time(),
                    plan_id,
                    PlanState.RUNNING,
                ),
            ).rowcount

        return bool(self._execute_write(record))

    def record_dispatch_deferred(self, plan_id: str, error: str) -> bool:
        def record(conn):
            row = conn.execute(
                "SELECT * FROM bestplan_plans WHERE plan_id=? AND state IN (?, ?)",
                (plan_id, PlanState.RUNNING, PlanState.WAITING),
            ).fetchone()
            if row is None:
                return 0
            if int(row["execution_protocol"] or 1) == 2:
                from .bestplan_proof import ProofLedger

                compatibility = (
                    {
                        "compatibility_error": "dispatch_deferred",
                        "compatibility_dispatch_state": "intent",
                    }
                    if (
                        row["current_phase"] == "captured"
                        and row["dispatch_state"] == "dispatching"
                        and isinstance(row["dispatch_owner"], str)
                        and bool(row["dispatch_owner"])
                    )
                    else {}
                )
                ProofLedger(self).append_advisory_in_transaction(
                    conn,
                    plan_id=plan_id,
                    kind="dispatch_deferred_advisory",
                    raw_output={"status": "dispatch_deferred", "detail": error},
                    output_source="model-broker",
                    **compatibility,
                )
                return 1
            return conn.execute(
                """UPDATE bestplan_plans SET dispatch_state='intent', error=?,
                   dispatch_updated_at=? WHERE plan_id=? AND state=?
                   AND dispatch_state='dispatching'""",
                (str(error), time.time(), plan_id, PlanState.RUNNING),
            ).rowcount

        return bool(self._execute_write(record))

    def mark_completed_unverified(self, plan_id: str, evidence: dict[str, Any]) -> bool:
        def complete(conn):
            row = conn.execute(
                "SELECT * FROM bestplan_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if row is None:
                return 0
            if int(row["execution_protocol"] or 1) == 2:
                try:
                    validated = _validate_stored_plan_row(row)
                except BestplanError:
                    return 0
                if (
                    isinstance(validated.contract, Mapping)
                    and validated.contract.get("schema")
                    == "hermes.bestplan.local-go.v1"
                ):
                    if row["state"] == PlanState.COMPLETED_LOCAL:
                        try:
                            decode_local_push_row(
                                row, _validate_stored_plan_row,
                            )
                        except LocalPushStateError:
                            return 0
                        return 1
                    if row["local_push_state"] == "prepared":
                        try:
                            decode_local_push_row(
                                row, _validate_stored_plan_row,
                            )
                        except LocalPushStateError:
                            return 0
                        # The local Git effect may have completed after the
                        # durable prepared record but before activation.  Git
                        # read-back, not the async wrapper, decides this row.
                        from .bestplan_proof import ProofLedger

                        ProofLedger(self).append_advisory_in_transaction(
                            conn,
                            plan_id=plan_id,
                            kind="local_effect_prepared_advisory",
                            raw_output={"status": "local_effect_prepared"},
                            output_source="async",
                            compatibility_dispatch_state="unknown",
                            compatibility_clear_dispatch_owner=True,
                        )
                        return 1
                    if row["state"] not in {
                        PlanState.RUNNING,
                        PlanState.WAITING,
                    }:
                        return 0
                    from .bestplan_proof import ProofLedger

                    ProofLedger(self).append_advisory_in_transaction(
                        conn,
                        plan_id=plan_id,
                        kind="local_execution_failed_advisory",
                        raw_output={"status": "local_execution_failed"},
                        output_source="async",
                        compatibility_error="dispatch_failed",
                        compatibility_dispatch_state="terminal",
                        compatibility_clear_dispatch_owner=True,
                    )
                    return conn.execute(
                        """UPDATE bestplan_plans
                           SET state=?
                           WHERE plan_id=? AND state IN (?, ?)""",
                        (
                            PlanState.FAILED,
                            plan_id,
                            PlanState.RUNNING,
                            PlanState.WAITING,
                        ),
                    ).rowcount
                if row["state"] not in {PlanState.RUNNING, PlanState.WAITING}:
                    return 0
                from .bestplan_proof import ProofLedger

                if row["current_phase"] == "captured":
                    compatibility = {
                        "compatibility_error": "recapture_required",
                        "compatibility_dispatch_state": "terminal",
                        "compatibility_clear_dispatch_owner": True,
                    }
                elif row["current_phase"] == "candidate_ready":
                    compatibility = {
                        "compatibility_dispatch_state": "terminal",
                        "compatibility_clear_dispatch_owner": True,
                    }
                else:
                    compatibility = {}
                ProofLedger(self).append_advisory_in_transaction(
                    conn,
                    plan_id=plan_id,
                    kind="async_terminal_advisory",
                    raw_output=evidence,
                    output_source="async",
                    **compatibility,
                )
                return 1
            if row["state"] not in {PlanState.RUNNING, PlanState.WAITING}:
                return 0
            # Protocol 1 can retain candidate evidence, but candidate freezing
            # is not implementation completion.  Keep the compatibility row
            # nonterminal and close only its finished dispatch attempt.
            return conn.execute(
                "UPDATE bestplan_plans SET state=?, dispatch_state='terminal', "
                "dispatch_owner=NULL, evidence_json=?, completed_at=NULL, "
                "dispatch_updated_at=?, error=NULL "
                "WHERE plan_id=? AND state IN (?, ?)",
                (
                    PlanState.RUNNING,
                    json.dumps(evidence, sort_keys=True),
                    time.time(),
                    plan_id,
                    PlanState.RUNNING,
                    PlanState.WAITING,
                ),
            ).rowcount

        return bool(self._execute_write(complete))

    def mark_completed_verified(self, plan_id: str) -> bool:
        """Reject the obsolete state-only verified transition for every plan."""

        return False


def capture_bestplan_response(
    response: str,
    *,
    session_id: str,
    workspace: str,
    topic: str | None = None,
    profile: str = "",
    baseline_fingerprint: Optional[str] = None,
    store: Optional[BestplanStore] = None,
    provisional: bool = False,
    config: Optional[dict[str, Any]] = None,
    authority_client: BestplanAuthorityClient | None = None,
    local_execution: bool = False,
    host_receipt_metadata: Mapping[str, Any] | None = None,
    host_receipt_warning: str | None = None,
) -> PlanCapture:
    """Validate and persist the explicit envelope in a /bestplan response.

    ``host_receipt_metadata`` is produced by the host orchestration path.  It
    is validated against the exact envelope body before any model identities
    are shown to a human.  Model-visible receipt text is only stripped or
    checked for consistency; it is never trusted as metadata input.
    """
    try:
        raw_envelope, plan, manifest = _extract_envelope(response)
        _v1_plan_constraints(plan, workspace=workspace)
    except BestplanError as exc:
        suffix = (
            "\n\n[Bestplan status: non-executable — the response did not contain "
            f"one valid machine envelope ({exc}).]"
        )
        return PlanCapture(False, suffix.strip(), error=str(exc))
    try:
        store = store or BestplanStore()
        plan_id = store.create_plan(
            _inline_text(topic),
            plan,
            session_id=session_id,
            profile=profile,
            workspace=workspace,
            baseline_fingerprint=baseline_fingerprint, raw_envelope=raw_envelope,
            provisional=provisional,
            config=config,
            authority_client=authority_client,
            local_execution=local_execution,
        )
        row = store.get_plan(plan_id)
        if row is None:
            raise BestplanError("persisted plan could not be read back")
        validated = _validate_stored_plan_row(row)
    except (BaselineFingerprintError, BestplanError) as exc:
        suffix = f"\n\n[Bestplan status: non-executable — {exc}.]"
        return PlanCapture(False, suffix.strip(), error=str(exc))
    digest = validated.approval_digest
    receipt_metadata = _bestplan_receipt_metadata(
        host_receipt_metadata,
        response,
        raw_envelope,
    )
    human = _render_human_plan(
        validated.plan,
        workspace=row["workspace"],
        plan_id=plan_id,
        contract=validated.contract,
        receipt_metadata=receipt_metadata,
        topic=topic,
    )
    warning = _render_host_receipt_warning(host_receipt_warning)
    if warning:
        human += f"\n\n{warning}"
    return PlanCapture(True, human, plan_id=plan_id, digest=digest)


def is_executable_bestplan_invocation(message: Any) -> bool:
    """Recognize only host-owned forms allowed to mint executable plans."""
    if not isinstance(message, str):
        return False

    # The canonical CLI queues BestPlan through the conversation loop with a
    # NUL-delimited host marker.  Preserve that identity for result capture,
    # but only for the exact marker shape the producer emits; malformed or
    # user-spoofed marker-like text must not gain BestPlan semantics.
    from .bestplan_orchestrator import TURN_MARKER, decode_bestplan_turn

    if message.startswith(TURN_MARKER):
        _task, marker_config, marker_error = decode_bestplan_turn(message)
        return marker_config is not None and marker_error is None

    stripped = message.lstrip()
    return bool(re.match(r"^/bestplan(?:\s|$)", stripped, re.IGNORECASE))


def is_bestplan_invocation(message: Any) -> bool:
    """Recognize raw, host-marked, or legacy expanded BestPlan turns."""
    if is_executable_bestplan_invocation(message):
        return True
    if not isinstance(message, str):
        return False

    stripped = message.lstrip()
    prefix = stripped[:500].casefold()
    return (
        prefix.startswith("[important: the user has invoked the ")
        and "bestplan" in prefix
        and " skill" in prefix
    )


def _history_has_executable_bestplan(history: list[dict[str, Any]]) -> bool:
    for item in reversed(list(history or [])):
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "")
        if (
            BESTPLAN_ENVELOPE_START in content
            or "Bestplan executable receipt:" in content
            or "Authoritative executable manifest" in content
            or "BestPlan status: planning-only" in content
        ):
            return True
        # Stop at the most recent unrelated user turn: an old planning answer
        # must not reserve the word "go" forever.
        if item.get("role") == "user" and content.strip().casefold() != "go":
            break
    return False


def unsupported_host_bestplan_before_model(
    message: Any,
    *,
    conversation_history: list[dict[str, Any]],
    host_name: str,
) -> ResolvedGo | None:
    """Reject only a plan-associated bare ``go`` on planning-only hosts."""
    if not isinstance(message, str) or not _is_go_trigger(message):
        return None
    if not _history_has_executable_bestplan(conversation_history):
        return None
    return ResolvedGo(
        True,
        "unsupported_host",
        reason=(
            f"{host_name} is planning-only for BestPlan V1; use a supported "
            "host and explicitly approve the host-rendered manifest"
        ),
    )


def unsupported_host_bestplan_after_model(
    result: dict[str, Any],
    *,
    invocation_message: Any,
    host_name: str,
) -> dict[str, Any]:
    """Remove executable authority from a /bestplan answer on unsupported hosts."""
    if not is_bestplan_invocation(invocation_message) or not isinstance(result, dict):
        return result
    suffix = (
        f"[BestPlan status: planning-only on {host_name}; no executable manifest "
        "was persisted and bare `go` cannot dispatch this plan here.]"
    )
    response = suffix
    updated = dict(result)
    updated["final_response"] = response
    messages = [dict(item) if isinstance(item, dict) else item for item in (updated.get("messages") or [])]
    for index in range(len(messages) - 1, -1, -1):
        item = messages[index]
        if isinstance(item, dict) and item.get("role") == "assistant":
            item["content"] = response
            break
    updated["messages"] = messages
    updated["bestplan_capture"] = {
        "executable": False,
        "plan_id": None,
        "digest": None,
        "error": "unsupported_host",
    }
    return updated


def run_planning_only_bestplan_turn(
    *,
    invocation_message: Any,
    conversation_history: list[dict[str, Any]],
    host_name: str,
    host_agent: Any,
    run_model_turn: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Shared production ingress for hosts without executable BestPlan V1."""
    blocked = unsupported_host_bestplan_before_model(
        invocation_message,
        conversation_history=conversation_history,
        host_name=host_name,
    )
    if blocked is not None:
        return blocked.to_agent_result(
            conversation_history=conversation_history,
            user_message=str(invocation_message),
            host_agent=host_agent,
        )
    return unsupported_host_bestplan_after_model(
        run_model_turn(),
        invocation_message=invocation_message,
        host_name=host_name,
    )


@contextmanager
def bind_bestplan_delivery_context(
    *, session_key: str, session_id: str, profile: str, hermes_home: str | Path,
):
    """Stack-bind the strict V1 delivery/profile identity for one host turn."""
    from gateway.session_context import bind_delivery_context, reset_delivery_context
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home = Path(hermes_home).expanduser().resolve()
    home_token = set_hermes_home_override(home)
    delivery_tokens = bind_delivery_context(
        session_key=session_key,
        session_id=session_id,
        ui_session_id=session_id,
        async_delivery=True,
        profile=profile,
        hermes_home=str(home),
        capability_version=BESTPLAN_HOST_CAPABILITY_VERSION,
    )
    try:
        yield
    finally:
        reset_delivery_context(delivery_tokens)
        reset_hermes_home_override(home_token)


def _valid_no_scope_receipt_metadata(metadata: Mapping[str, Any]) -> bool:
    """Require affirmative quorum authority before a failure can cancel plans."""
    from .bestplan_orchestrator import _valid_v2_receipt_metadata

    receipt = dict(metadata)
    if not _valid_v2_receipt_metadata(receipt, ""):
        return False
    if (
        receipt.get("status") != "failed"
        or receipt.get("reason_code") != "no_in_scope_implementation"
    ):
        return False
    attempts = receipt["attempts"]
    successes = sum(item["status"] == "success" for item in attempts)
    if successes < receipt["quorum_required"]:
        return False
    synthesizer = receipt["synthesizer"]
    return (
        synthesizer.get("status") == "success"
        and synthesizer.get("reason_code") is None
    )


def capture_bestplan_agent_result(
    result: dict[str, Any],
    *,
    invocation_message: Any,
    topic: str | None = None,
    session_id: str,
    workspace: str,
    profile: str = "",
    baseline_fingerprint: Optional[str] = None,
    store: Optional[BestplanStore] = None,
    host_agent: Any = None,
    provisional: bool = False,
    config: Optional[dict[str, Any]] = None,
    authority_client: BestplanAuthorityClient | None = None,
    local_execution: bool = False,
) -> dict[str, Any]:
    """Attach the host-validated executable receipt to a planning result."""
    if not is_executable_bestplan_invocation(invocation_message) or not isinstance(result, dict):
        return result
    injected_client = authority_client
    if injected_client is None and host_agent is not None:
        injected_client = getattr(host_agent, "bestplan_authority_client", None)
    host_receipt_metadata = (
        result.get("bestplan_receipt_metadata")
        if isinstance(result.get("bestplan_receipt_metadata"), Mapping)
        else (
            getattr(host_agent, "_bestplan_receipt_metadata", None)
            if host_agent is not None
            else None
        )
    )
    if (
        result.get("failed") is True
        and result.get("turn_exit_reason") == "bestplan"
        and isinstance(host_receipt_metadata, Mapping)
    ):
        receipt_metadata = dict(host_receipt_metadata)
        if _valid_no_scope_receipt_metadata(receipt_metadata):
            if any(
                str(item).startswith("persist_session:")
                for item in (result.get("cleanup_errors") or [])
            ):
                raise RuntimeError(
                    "BestPlan no-scope response persistence failed"
                )
            persist = getattr(host_agent, "_persist_session", None)
            if not callable(persist) or persist(
                list(result.get("messages") or []),
                None,
                rewrite=True,
            ) is not True:
                raise RuntimeError(
                    "BestPlan no-scope response persistence unavailable"
                )
            cutoff = time.time()
            expected_baseline = (
                baseline_fingerprint
                if baseline_fingerprint is not None
                else compute_baseline_fingerprint(workspace)
            )
            owns_store = store is None
            outcome_store = store or BestplanStore()
            try:
                outcome_store.supersede_unstarted_plans(
                    session_id=session_id,
                    profile=profile,
                    workspace=workspace,
                    baseline_fingerprint=expected_baseline,
                    before=cutoff,
                    local_execution=local_execution,
                )
            finally:
                if owns_store:
                    outcome_store.close()
            updated = dict(result)
            updated["bestplan_capture"] = {
                "executable": False,
                "plan_id": None,
                "digest": None,
                "error": "no_in_scope_implementation",
            }
            return updated
    capture = capture_bestplan_response(
        str(result.get("final_response") or ""),
        session_id=session_id,
        profile=profile,
        workspace=workspace,
        topic=(
            _inline_text(topic)
            if topic is not None
            else _bestplan_topic_from_invocation(invocation_message)
        ),
        baseline_fingerprint=baseline_fingerprint,
        store=store,
        provisional=provisional,
        config=config if config is not None else _load_config(),
        authority_client=injected_client,
        local_execution=local_execution,
        host_receipt_metadata=host_receipt_metadata,
        host_receipt_warning=(
            result.get("bestplan_receipt_warning")
            if isinstance(result.get("bestplan_receipt_warning"), str)
            else (
                getattr(host_agent, "_bestplan_receipt_warning", None)
                if host_agent is not None
                else None
            )
        ),
    )
    updated = dict(result)
    updated["final_response"] = capture.response
    messages = list(updated.get("messages") or [])
    for index in range(len(messages) - 1, -1, -1):
        item = messages[index]
        if isinstance(item, dict) and item.get("role") == "assistant":
            replacement = dict(item)
            replacement["content"] = capture.response
            if item.get("content") != capture.response:
                # This is a host-owned replacement of a model response, not a
                # new assistant turn.  Do not retain either the append-flush
                # durability marker or a provider-wire sidecar whose bytes
                # describe the superseded response.
                replacement.pop("_db_persisted", None)
                replacement.pop("api_content", None)
            messages[index] = replacement
            break
    updated["messages"] = messages
    updated["bestplan_capture"] = {
        "executable": capture.executable,
        "plan_id": capture.plan_id,
        "digest": capture.digest,
        "error": capture.error,
    }
    if host_agent is not None:
        from agent.agent_runtime_helpers import repair_message_sequence

        repair_message_sequence(host_agent, messages)
        if callable(getattr(host_agent, "_persist_session", None)):
            persisted = host_agent._persist_session(
                messages,
                None,
                rewrite=True,
            )
            updated["bestplan_capture"]["receipt_persisted"] = persisted is True
    return updated


def _load_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly
        return load_config_readonly() or {}
    except Exception:
        logger.debug("bestplan config load failed", exc_info=True)
        return {}


def is_go_enabled(config: Optional[dict[str, Any]] = None) -> bool:
    cfg = config if config is not None else _load_config()
    return bool((cfg.get("autonomy") or {}).get("go_enabled"))


def recover_bestplan_dispatch_outbox(store: Optional[BestplanStore] = None) -> int:
    """Reconcile only dispatch attempts proven to belong to a dead process."""
    return (store or BestplanStore()).recover_dead_dispatch_owners()


def _configured_lane_error(tasks: list[dict[str, Any]], config: dict[str, Any]) -> Optional[str]:
    lanes = ((config.get("delegation") or {}).get("lanes") or {})
    if not isinstance(lanes, dict):
        lanes = {}
    for route in sorted({str(task.get("route") or "") for task in tasks}):
        lane = lanes.get(route)
        if not isinstance(lane, dict):
            return f"delegation.lanes.{route} is not configured"
        if not str(lane.get("provider") or "").strip():
            return f"delegation.lanes.{route}.provider is required"
        if not str(lane.get("model") or "").strip():
            return f"delegation.lanes.{route}.model is required"
    return None


def _delegation_ids(payload: Any) -> list[str]:
    found: list[str] = []
    if isinstance(payload, dict):
        one = payload.get("delegation_id")
        if isinstance(one, str) and one:
            found.append(one)
        many = payload.get("delegation_ids")
        if isinstance(many, list):
            found.extend(str(item) for item in many if str(item or ""))
        for key in ("results", "dispatches"):
            values = payload.get(key)
            if isinstance(values, list):
                for value in values:
                    found.extend(_delegation_ids(value))
    return list(dict.fromkeys(found))


class _FrozenHandoffSequence(tuple):
    """Tuple semantics with relationship-compatible sequence equality."""

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (list, tuple)):
            return tuple(self) == tuple(other)
        return False

    __hash__ = tuple.__hash__


def _immutable_bestplan_handoff(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({
            str(key): _immutable_bestplan_handoff(item)
            for key, item in value.items()
        })
    if isinstance(value, (list, tuple)):
        return _FrozenHandoffSequence(
            _immutable_bestplan_handoff(item) for item in value
        )
    return value


def try_resolve_go(
    message: str,
    *,
    session_id: str,
    workspace: str,
    parent_agent: Any,
    profile: str = "",
    baseline_fingerprint: Optional[str] = None,
    config: Optional[dict[str, Any]] = None,
    store: Optional[BestplanStore] = None,
    delegate: Optional[Callable[..., Any]] = None,
    runtime_resolver: Optional[Callable[[list[dict[str, Any]], Any], list[dict[str, Any]]]] = None,
    strict_dispatcher: Optional[Callable[..., Any]] = None,
    candidate_host_runtime: Any = None,
    authority_client: Any = None,
) -> ResolvedGo:
    """Resolve bare ``go`` before the model loop, failing closed around a plan."""
    cfg = config if config is not None else _load_config()
    go_enabled = is_go_enabled(cfg)
    if not _is_go_trigger(message):
        return ResolvedGo(False, "not_a_trigger", reason="only bare go is recognized")

    store = store or BestplanStore()
    recover_bestplan_dispatch_outbox(store)
    candidates = store.list_for_session(session_id)
    if not candidates:
        if not go_enabled:
            return ResolvedGo(
                False, "disabled", reason="autonomy.go_enabled=false"
            )
        return ResolvedGo(False, "no_plan", reason="no pending plan exists for this session")

    expected_workspace = _canonical_workspace(workspace)
    expected_profile = str(profile)
    context_candidates = [
        row for row in candidates
        if row["profile"] == expected_profile and row["workspace"] == expected_workspace
    ]
    if not context_candidates:
        return ResolvedGo(True, "context_mismatch", reason="pending plan belongs to another profile or workspace")

    baseline = baseline_fingerprint or compute_baseline_fingerprint(workspace)
    exact = [row for row in context_candidates if row["baseline_fingerprint"] == baseline]
    if not exact:
        return ResolvedGo(True, "stale", reason="pending plan baseline no longer matches")
    if len(exact) != 1:
        return ResolvedGo(True, "ambiguous", reason=f"{len(exact)} plans match this turn")

    candidate = exact[0]
    plan_id = candidate["plan_id"]
    protocol2 = int(candidate.get("execution_protocol") or 1) == 2
    legacy_injected_p1 = not protocol2 and (
        delegate is not None or strict_dispatcher is not None
    )
    try:
        validated = _validate_stored_plan_row(candidate)
    except Exception as exc:
        error = "invalid_plan" if protocol2 else str(exc)
        return ResolvedGo(
            True, "invalid_plan", plan_id=plan_id, reason=error, error=error,
        )
    local_contract = (
        protocol2
        and isinstance(validated.contract, Mapping)
        and validated.contract.get("schema") == "hermes.bestplan.local-go.v1"
        and validated.contract.get("mode") == "local_main"
    )
    if not go_enabled and not local_contract:
        return ResolvedGo(
            False, "disabled", reason="autonomy.go_enabled=false"
        )
    if local_contract:
        try:
            pending_pushes = [
                row
                for row in store.list_active_local_pushes(session_id)
                if row.get("profile") == expected_profile
                and row.get("workspace") == expected_workspace
            ]
        except Exception:
            return ResolvedGo(
                True,
                "push_state_unavailable",
                plan_id=plan_id,
                reason="the pending local push state is unavailable",
            )
        if pending_pushes:
            return ResolvedGo(
                True,
                "push_pending",
                plan_id=plan_id,
                reason="reply push or no before running another local plan",
            )
    if protocol2 and str(candidate.get("current_phase") or "captured") != "captured":
        return ResolvedGo(
            True,
            "already_advanced",
            plan_id=plan_id,
            delegation_id=str(
                candidate.get("dispatch_id") or f"bestplan-{plan_id}"
            ),
            reason="protocol-2 plan already advanced beyond candidate dispatch",
        )
    if candidate["state"] == PlanState.WAITING:
        return ResolvedGo(True, "already_claimed", plan_id=plan_id, reason="plan was already dispatched")

    try:
        tasks = _plan_to_delegate_tasks(
            validated.plan, workspace=expected_workspace,
        )
    except Exception as exc:
        error = "invalid_plan" if protocol2 else str(exc)
        return ResolvedGo(
            True, "invalid_plan", plan_id=plan_id, reason=error, error=error,
        )

    if parent_agent is None:
        return ResolvedGo(True, "dispatch_unavailable", plan_id=plan_id, reason="live parent agent is required")
    effective_host_runtime = (
        candidate_host_runtime
        if candidate_host_runtime is not None
        else getattr(parent_agent, "candidate_host_runtime", None)
    )
    effective_authority_client = (
        authority_client
        if authority_client is not None
        else getattr(parent_agent, "bestplan_authority_client", None)
    )
    state_db_path = store.state_db_path
    host_runtime_projection: dict[str, Any] = {}
    local_execution_runtime: Any = None
    authority_bindings: Any = None
    review_authority_bindings: Any = None

    def resolve_local_review_authorities() -> Any:
        from .bestplan_local import build_local_review_authority_bindings

        review_tasks = _local_review_runtime_tasks(expected_workspace)
        if runtime_resolver is None:
            from tools.delegate_tool import resolve_bestplan_runtime_specs

            review_runtimes = resolve_bestplan_runtime_specs(
                review_tasks,
                parent_agent,
                execution_protocol=2,
            )
        else:
            review_runtimes = runtime_resolver(review_tasks, parent_agent)
        return build_local_review_authority_bindings(review_runtimes)
    if local_contract and state_db_path is None:
        return ResolvedGo(
            True,
            "candidate_runtime_unavailable",
            plan_id=plan_id,
            reason="candidate_runtime_unavailable",
            error="candidate_runtime_unavailable",
        )
    if not legacy_injected_p1 and not local_contract:
        try:
            from tools.delegate_tool import (
                BestplanHostRuntime,
                _BestplanPreflightError,
                _bestplan_host_runtime_projection,
                _validate_bestplan_host_runtime,
            )

            if (
                not isinstance(effective_host_runtime, BestplanHostRuntime)
                or effective_authority_client is None
                or state_db_path is None
                or validated.source_snapshot is None
            ):
                raise _BestplanPreflightError("candidate_runtime_unavailable")
            _validate_bestplan_host_runtime(
                effective_host_runtime,
                source_snapshot=validated.source_snapshot,
                promotion_contract=validated.contract if protocol2 else None,
            )
            host_runtime_projection = _bestplan_host_runtime_projection(
                effective_host_runtime
            )
        except Exception:
            return ResolvedGo(
                True,
                "candidate_runtime_unavailable",
                plan_id=plan_id,
                reason="candidate_runtime_unavailable",
                error="candidate_runtime_unavailable" if protocol2 else None,
            )
    try:
        from gateway.session_context import async_delivery_supported
        if not async_delivery_supported():
            return ResolvedGo(True, "async_unsupported", plan_id=plan_id, reason="host cannot deliver detached completion")
    except Exception:
        return ResolvedGo(True, "async_context_error", plan_id=plan_id, reason="delivery capability could not be verified")

    resolved_runtimes: list[dict[str, Any]]
    if candidate["state"] == PlanState.RUNNING:
        dispatch_id = str(candidate.get("dispatch_id") or f"bestplan-{plan_id}")
        if candidate.get("dispatch_state") == "dispatching":
            return ResolvedGo(
                True, "dispatch_in_flight", plan_id=plan_id,
                delegation_id=dispatch_id,
                reason="another idempotent dispatch attempt owns the durable intent",
            )
        try:
            stored_runtimes = json.loads(candidate.get("resolved_runtime_json") or "[]")
        except Exception:
            stored_runtimes = []
        if (
            not legacy_injected_p1
            and not local_contract
            and (
                not isinstance(stored_runtimes, list)
                or len(stored_runtimes) != len(tasks)
                or any(
                    not isinstance(item, dict)
                    or any(
                        item.get(key) != value
                        for key, value in host_runtime_projection.items()
                    )
                    for item in stored_runtimes
                )
            )
        ):
            return ResolvedGo(
                True,
                "candidate_runtime_unavailable",
                plan_id=plan_id,
                reason="candidate_runtime_unavailable",
                error="candidate_runtime_unavailable" if protocol2 else None,
            )
        try:
            if runtime_resolver is None:
                from tools.delegate_tool import resolve_bestplan_runtime_specs
                resolved_runtimes = resolve_bestplan_runtime_specs(
                    tasks,
                    parent_agent,
                    expected=stored_runtimes,
                    execution_protocol=validated.execution_protocol,
                )
            else:
                resolved_runtimes = runtime_resolver(tasks, parent_agent)
        except Exception as exc:
            error = "lane_unavailable" if protocol2 else str(exc)
            return ResolvedGo(
                True, "lane_unavailable", plan_id=plan_id,
                reason=error, error=error if protocol2 else None,
            )
        if protocol2:
            try:
                if local_contract:
                    from .bestplan_local import build_local_authority_bindings

                    authority_bindings = build_local_authority_bindings(
                        resolved_runtimes
                    )
                    review_authority_bindings = (
                        resolve_local_review_authorities()
                    )
                resolved_runtimes = _bind_v2_candidate_toolsets(
                    resolved_runtimes, tasks,
                )
                resolved_runtimes = _filter_v2_runtime_execution(
                    resolved_runtimes
                )
            except Exception:
                return ResolvedGo(
                    True,
                    "lane_unavailable",
                    plan_id=plan_id,
                    reason="lane_unavailable",
                    error="lane_unavailable",
                )
        if local_contract:
            try:
                from .bestplan_local import build_local_execution_runtime
                from tools.delegate_tool import (
                    _bestplan_host_runtime_projection,
                    _validate_bestplan_host_runtime,
                )

                local_execution_runtime = build_local_execution_runtime(
                    plan_id=plan_id,
                    snapshot=validated.source_snapshot,
                    manifest=validated.manifest,
                    contract=validated.contract,
                    controller_python=Path(sys.executable),
                    deadline=time.monotonic() + 60.0,
                )
                effective_host_runtime = (
                    local_execution_runtime.candidate_runtime
                )
                _validate_bestplan_host_runtime(
                    effective_host_runtime,
                    source_snapshot=validated.source_snapshot,
                    promotion_contract=validated.contract,
                )
                host_runtime_projection = _bestplan_host_runtime_projection(
                    effective_host_runtime
                )
            except Exception:
                return ResolvedGo(
                    True,
                    "candidate_runtime_unavailable",
                    plan_id=plan_id,
                    reason="candidate_runtime_unavailable",
                    error="candidate_runtime_unavailable",
                )
            if (
                not isinstance(stored_runtimes, list)
                or len(stored_runtimes) != len(tasks)
                or any(
                    not isinstance(item, dict)
                    or any(
                        item.get(key) != value
                        for key, value in host_runtime_projection.items()
                    )
                    for item in stored_runtimes
                )
            ):
                return ResolvedGo(
                    True,
                    "candidate_runtime_unavailable",
                    plan_id=plan_id,
                    reason="candidate_runtime_unavailable",
                    error="candidate_runtime_unavailable",
                )
    else:
        if runtime_resolver is None and delegate is not None:
            lane_error = _configured_lane_error(tasks, cfg)
            if lane_error:
                error = "lane_unavailable" if protocol2 else lane_error
                return ResolvedGo(
                    True, "lane_unavailable", plan_id=plan_id,
                    reason=error, error=error if protocol2 else None,
                )
            lanes = ((cfg.get("delegation") or {}).get("lanes") or {})
            resolved_runtimes = [
                {
                    "route": task["route"],
                    "provider": lanes[task["route"]]["provider"],
                    "model": lanes[task["route"]]["model"],
                }
                for task in tasks
            ]
        else:
            try:
                if runtime_resolver is None:
                    from tools.delegate_tool import resolve_bestplan_runtime_specs
                    resolved_runtimes = resolve_bestplan_runtime_specs(
                        tasks,
                        parent_agent,
                        execution_protocol=validated.execution_protocol,
                    )
                else:
                    resolved_runtimes = runtime_resolver(tasks, parent_agent)
            except Exception as exc:
                error = "lane_unavailable" if protocol2 else str(exc)
                return ResolvedGo(
                    True, "lane_unavailable", plan_id=plan_id,
                    reason=error, error=error if protocol2 else None,
                )
        resolved_runtimes_for_storage = resolved_runtimes
        if protocol2:
            try:
                if local_contract:
                    from .bestplan_local import build_local_authority_bindings

                    authority_bindings = build_local_authority_bindings(
                        resolved_runtimes
                    )
                    review_authority_bindings = (
                        resolve_local_review_authorities()
                    )
                resolved_runtimes = _bind_v2_candidate_toolsets(
                    resolved_runtimes, tasks,
                )
                resolved_runtimes = _filter_v2_runtime_execution(
                    resolved_runtimes
                )
                resolved_runtimes_for_storage = sanitize_runtime_metadata(
                    resolved_runtimes,
                    execution_protocol=2,
                )
            except Exception:
                return ResolvedGo(
                    True,
                    "lane_unavailable",
                    plan_id=plan_id,
                    reason="lane_unavailable",
                    error="lane_unavailable",
                )
        if local_contract:
            try:
                from .bestplan_local import build_local_execution_runtime
                from tools.delegate_tool import (
                    _bestplan_host_runtime_projection,
                    _validate_bestplan_host_runtime,
                )

                local_execution_runtime = build_local_execution_runtime(
                    plan_id=plan_id,
                    snapshot=validated.source_snapshot,
                    manifest=validated.manifest,
                    contract=validated.contract,
                    controller_python=Path(sys.executable),
                    deadline=time.monotonic() + 60.0,
                )
                effective_host_runtime = (
                    local_execution_runtime.candidate_runtime
                )
                _validate_bestplan_host_runtime(
                    effective_host_runtime,
                    source_snapshot=validated.source_snapshot,
                    promotion_contract=validated.contract,
                )
                host_runtime_projection = _bestplan_host_runtime_projection(
                    effective_host_runtime
                )
            except Exception:
                return ResolvedGo(
                    True,
                    "candidate_runtime_unavailable",
                    plan_id=plan_id,
                    reason="candidate_runtime_unavailable",
                    error="candidate_runtime_unavailable",
                )
        resolved_runtimes_for_storage = [
            {**dict(item), **host_runtime_projection}
            for item in resolved_runtimes_for_storage
        ]
        claimed = store.prepare_dispatch_intent(
            plan_id, baseline, resolved_runtimes=resolved_runtimes_for_storage,
            session_id=session_id, profile=expected_profile, workspace=expected_workspace,
        )
        if claimed is None:
            return ResolvedGo(True, "already_claimed", plan_id=plan_id, reason="atomic intent claim lost or validation changed")
        candidate = claimed

    dispatch_id = str(candidate.get("dispatch_id") or f"bestplan-{plan_id}")
    if not store.begin_dispatch_attempt(plan_id):
        row = store.get_plan(plan_id) or {}
        if row.get("state") == PlanState.WAITING:
            return ResolvedGo(True, "already_claimed", plan_id=plan_id, delegation_id=dispatch_id)
        return ResolvedGo(
            True, "dispatch_in_flight", plan_id=plan_id, delegation_id=dispatch_id,
            reason="durable dispatch attempt already owned",
        )

    if strict_dispatcher is None:
        if delegate is not None:
            strict_dispatcher = lambda **kwargs: delegate(
                tasks=kwargs["tasks"], parent_agent=kwargs["parent_agent"], background=True,
            )
        else:
            from tools.delegate_tool import dispatch_bestplan_tasks_async
            strict_dispatcher = dispatch_bestplan_tasks_async
    try:
        if legacy_injected_p1:
            raw_result = strict_dispatcher(
                tasks=tasks,
                parent_agent=parent_agent,
                dispatch_id=dispatch_id,
                plan_id=plan_id,
                workspace=expected_workspace,
                resolved_runtimes=resolved_runtimes,
            )
        else:
            handoff_tasks = _immutable_bestplan_handoff(tasks)
            handoff_contract = (
                _immutable_bestplan_handoff(validated.contract)
                if protocol2
                else None
            )
            raw_result = strict_dispatcher(
                tasks=handoff_tasks,
                parent_agent=parent_agent,
                dispatch_id=dispatch_id,
                plan_id=plan_id,
                workspace=expected_workspace,
                resolved_runtimes=resolved_runtimes,
                execution_protocol=validated.execution_protocol,
                source_snapshot=validated.source_snapshot,
                approval_digest=validated.approval_digest,
                promotion_contract=handoff_contract,
                promotion_contract_digest=(
                    str(candidate.get("promotion_contract_digest") or "")
                    if protocol2
                    else ""
                ),
                promotion_mode=(
                    str(
                        validated.contract.get(
                            "mode" if local_contract else "promotion_mode"
                        )
                        or ""
                    )
                    if protocol2 and validated.contract is not None
                    else None
                ),
                execution_plan=validated.plan if local_contract else None,
                local_execution_runtime=(
                    local_execution_runtime if local_contract else None
                ),
                candidate_host_runtime=effective_host_runtime,
                authority_client=(
                    None if local_contract else effective_authority_client
                ),
                authority_bindings=(
                    authority_bindings if local_contract else None
                ),
                review_authority_bindings=(
                    review_authority_bindings if local_contract else None
                ),
                raw_request=str(candidate.get("raw_request") or ""),
                state_db_path=state_db_path,
            )
        result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
        if not isinstance(result, dict) or result.get("status") != "dispatched":
            raw_error = (
                (result or {}).get("error") if isinstance(result, dict) else result
            )
            error = "dispatch_failed" if protocol2 else str(raw_error)
            if isinstance(result, dict) and result.get("status") == "rejected":
                store.record_dispatch_deferred(
                    plan_id, raw_error if protocol2 else error
                )
                return ResolvedGo(
                    True, "dispatch_deferred", plan_id=plan_id,
                    delegation_id=dispatch_id,
                    reason="dispatch_deferred" if protocol2 else error,
                )
            store.record_dispatch_failure(
                plan_id, raw_error if protocol2 else error
            )
            return ResolvedGo(
                True, "dispatch_failed", plan_id=plan_id,
                delegation_id=dispatch_id, reason=error, error=error,
            )
        delegation_ids = _delegation_ids(result)
        if not delegation_ids:
            raise BestplanError("delegate_task returned no delegation id")
    except Exception as exc:
        error = "dispatch_unknown" if protocol2 else str(exc)
        store.record_dispatch_unknown(plan_id, error)
        return ResolvedGo(
            True, "possibly_dispatched", plan_id=plan_id,
            delegation_id=dispatch_id, reason=error, error=error,
        )

    if not store.record_dispatch(
        plan_id,
        delegation_ids=delegation_ids,
        sandbox_workspace=str(result.get("sandbox_workspace") or ""),
    ):
        return ResolvedGo(True, "dispatch_state_error", plan_id=plan_id, reason="delegation id was not persisted")
    return ResolvedGo(
        True,
        "waiting",
        plan_id=plan_id,
        delegation_id=dispatch_id if protocol2 else delegation_ids[0],
    )
