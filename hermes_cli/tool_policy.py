"""Provider-neutral contract for required tool-dispatch policies.

The callback payload contains only JSON-shaped tool data and execution
identity: ``tool_name``, ``original_args``, ``effective_args``, ``task_id``,
``session_id``, ``turn_id``, ``tool_call_id``, prepared cwd metadata, and a
``policy_binding``. The binding authorizes the final execution shape, so it
covers a schema version, the effective arguments, all execution identities,
and the prepared cwd metadata. ``original_args`` is visible for audit but is
not authorization input.

This module defines data and validation only. Plugin registration, deadlines,
quarantine, and final dispatch enforcement are implemented by later layers.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextvars import copy_context
from dataclasses import dataclass, field
from typing import TypeAlias

from tools.daemon_pool import DaemonThreadPoolExecutor

POLICY_SCHEMA_VERSION = 1
MAX_POLICY_BLOCK_MESSAGE_BYTES = 1_000
TOOL_DISPATCH_CONFORMANCE_TOOL_NAME = "__hermes_policy_status_conformance__"

JSONValue: TypeAlias = (
    None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
)


class PolicyDecisionCode:
    """Stable codes safe to expose without callback or exception details."""

    ALLOWED = "policy_allowed"
    BLOCKED = "policy_blocked"
    MALFORMED = "policy_malformed_decision"
    NON_EXPLICIT = "policy_non_explicit_action"
    BINDING_MISSING = "policy_binding_missing"
    BINDING_MISMATCH = "policy_binding_mismatch"
    EMPTY_BLOCK_MESSAGE = "policy_empty_block_message"
    INVALID_BLOCK_MESSAGE = "policy_invalid_block_message"


class RequiredPolicyFailureCode:
    """Stable codes for failures enforcing ``tool_dispatch`` required policies."""

    CONFIG_INVALID = "required_policy_config_invalid"
    PLUGIN_MISSING = "required_policy_plugin_missing"
    PLUGIN_DISABLED = "required_policy_plugin_disabled"
    PLUGIN_LOAD_ERROR = "required_policy_plugin_load_error"
    REGISTRATION_MISSING = "required_policy_registration_missing"
    CALLBACK_ERROR = "required_policy_callback_error"
    TIMEOUT = "required_policy_timeout"
    EXECUTOR_SATURATED = "required_policy_executor_saturated"
    QUARANTINED = "required_policy_quarantined"


def _validate_json_value(value: object, path: str) -> None:
    if value is None or type(value) in (bool, int, str):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise TypeError(f"{path} must contain only finite JSON numbers")
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if type(value) is dict:
        for index, (key, item) in enumerate(value.items()):
            if type(key) is not str:
                raise TypeError(f"{path} must contain only string object keys")
            _validate_json_value(item, f"{path}.value[{index}]")
        return
    raise TypeError(f"{path} must be JSON-shaped")


def _require_string(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a string")
    return value


def _require_args(value: object, field_name: str) -> dict[str, JSONValue]:
    if type(value) is not dict:
        raise TypeError(f"{field_name} must be a JSON object")
    _validate_json_value(value, field_name)
    return value


def _clone_json_object(value: dict[str, JSONValue]) -> dict[str, JSONValue]:
    return json.loads(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    )


def _truncate_utf8(message: str) -> str:
    encoded = message.encode("utf-8")
    if len(encoded) <= MAX_POLICY_BLOCK_MESSAGE_BYTES:
        return message
    return encoded[:MAX_POLICY_BLOCK_MESSAGE_BYTES].decode("utf-8", errors="ignore")


@dataclass(frozen=True, slots=True)
class PreparedToolRuntime:
    effective_cwd: str | None
    effective_cwd_source: str
    effective_cwd_authoritative: bool

    def __post_init__(self) -> None:
        if self.effective_cwd is not None and type(self.effective_cwd) is not str:
            raise TypeError("effective_cwd must be a string or None")
        _require_string(self.effective_cwd_source, "effective_cwd_source")
        if type(self.effective_cwd_authoritative) is not bool:
            raise TypeError("effective_cwd_authoritative must be a boolean")


@dataclass(frozen=True, slots=True)
class ToolDispatchPolicyInput:
    tool_name: str
    original_args: dict[str, JSONValue]
    effective_args: dict[str, JSONValue]
    task_id: str
    session_id: str
    turn_id: str
    tool_call_id: str
    effective_cwd: str | None
    effective_cwd_source: str
    effective_cwd_authoritative: bool
    policy_binding: str

    def __post_init__(self) -> None:
        _validate_dispatch_fields(
            tool_name=self.tool_name,
            original_args=self.original_args,
            effective_args=self.effective_args,
            task_id=self.task_id,
            session_id=self.session_id,
            turn_id=self.turn_id,
            tool_call_id=self.tool_call_id,
            effective_cwd=self.effective_cwd,
            effective_cwd_source=self.effective_cwd_source,
            effective_cwd_authoritative=self.effective_cwd_authoritative,
        )
        if not _is_policy_binding(self.policy_binding):
            raise ValueError("policy_binding must be a lowercase SHA-256 digest")
        expected = compute_policy_binding(self)
        if not hmac.compare_digest(self.policy_binding, expected):
            raise ValueError("policy_binding does not match the dispatch input")
        object.__setattr__(self, "original_args", _clone_json_object(self.original_args))
        object.__setattr__(self, "effective_args", _clone_json_object(self.effective_args))

    def to_callback_payload(self) -> dict[str, JSONValue]:
        return {
            "tool_name": self.tool_name,
            "original_args": _clone_json_object(self.original_args),
            "effective_args": _clone_json_object(self.effective_args),
            "task_id": self.task_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "tool_call_id": self.tool_call_id,
            "effective_cwd": self.effective_cwd,
            "effective_cwd_source": self.effective_cwd_source,
            "effective_cwd_authoritative": self.effective_cwd_authoritative,
            "policy_binding": self.policy_binding,
        }


@dataclass(frozen=True, slots=True)
class ToolPolicyRegistration:
    plugin_key: str
    policy_name: str
    callback: Callable[[Mapping[str, JSONValue]], object]
    timeout_ms: int


@dataclass(frozen=True, slots=True)
class PluginMiddlewareRegistration:
    """Registration-time middleware ownership without source-code inference."""

    plugin_key: str
    kind: str
    callback: Callable[..., object]

    def __call__(self, **kwargs: object) -> object:
        return self.callback(**kwargs)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    is_allowed: bool
    policy_code: str
    message: str = ""

    def allows(self) -> bool:
        return self.is_allowed


@dataclass(frozen=True, slots=True)
class ToolPolicyBlock:
    policy: str
    policy_code: str
    message: str
    status: str = field(default="blocked", init=False)
    error_type: str = field(default="required_policy_block", init=False)

    def __post_init__(self) -> None:
        _require_string(self.policy, "policy")
        _require_string(self.policy_code, "policy_code")
        if type(self.message) is not str or not self.message.strip():
            raise ValueError("message must be a non-empty string")
        object.__setattr__(self, "message", _truncate_utf8(self.message.strip()))

    def to_result(self) -> dict[str, str]:
        return {
            "status": self.status,
            "error_type": self.error_type,
            "policy": self.policy,
            "policy_code": self.policy_code,
            "message": self.message,
        }


_BINDING_FIELDS = frozenset(
    {
        "tool_name",
        "effective_args",
        "task_id",
        "session_id",
        "turn_id",
        "tool_call_id",
        "effective_cwd",
        "effective_cwd_source",
        "effective_cwd_authoritative",
    }
)


def _validate_dispatch_fields(
    *,
    tool_name: object,
    original_args: object,
    effective_args: object,
    task_id: object,
    session_id: object,
    turn_id: object,
    tool_call_id: object,
    effective_cwd: object,
    effective_cwd_source: object,
    effective_cwd_authoritative: object,
) -> None:
    _require_string(tool_name, "tool_name")
    _require_args(original_args, "original_args")
    _require_args(effective_args, "effective_args")
    for name, value in (
        ("task_id", task_id),
        ("session_id", session_id),
        ("turn_id", turn_id),
        ("tool_call_id", tool_call_id),
        ("effective_cwd_source", effective_cwd_source),
    ):
        _require_string(value, name)
    if effective_cwd is not None and type(effective_cwd) is not str:
        raise TypeError("effective_cwd must be a string or None")
    if type(effective_cwd_authoritative) is not bool:
        raise TypeError("effective_cwd_authoritative must be a boolean")


def _binding_values(
    payload: ToolDispatchPolicyInput | Mapping[str, object],
) -> dict[str, object]:
    if isinstance(payload, ToolDispatchPolicyInput):
        return {name: getattr(payload, name) for name in _BINDING_FIELDS}
    if not isinstance(payload, Mapping):
        raise TypeError("policy binding input must be a mapping")
    try:
        keys = set(payload.keys())
    except Exception:
        raise TypeError("policy binding input must expose stable keys") from None
    if keys != _BINDING_FIELDS:
        raise TypeError("policy binding input has missing or unexpected fields")
    return {name: payload[name] for name in _BINDING_FIELDS}


def compute_policy_binding(
    payload: ToolDispatchPolicyInput | Mapping[str, object],
) -> str:
    values = _binding_values(payload)
    _validate_dispatch_fields(
        original_args={},
        **values,
    )
    canonical = json.dumps(
        {"schema_version": POLICY_SCHEMA_VERSION, **values},
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_tool_dispatch_policy_input(
    *,
    tool_name: str,
    original_args: dict[str, JSONValue],
    effective_args: dict[str, JSONValue],
    task_id: str,
    session_id: str,
    turn_id: str,
    tool_call_id: str,
    prepared_runtime: PreparedToolRuntime,
) -> ToolDispatchPolicyInput:
    if not isinstance(prepared_runtime, PreparedToolRuntime):
        raise TypeError("prepared_runtime must be a PreparedToolRuntime")
    _validate_dispatch_fields(
        tool_name=tool_name,
        original_args=original_args,
        effective_args=effective_args,
        task_id=task_id,
        session_id=session_id,
        turn_id=turn_id,
        tool_call_id=tool_call_id,
        effective_cwd=prepared_runtime.effective_cwd,
        effective_cwd_source=prepared_runtime.effective_cwd_source,
        effective_cwd_authoritative=prepared_runtime.effective_cwd_authoritative,
    )
    original_snapshot = _clone_json_object(original_args)
    effective_snapshot = _clone_json_object(effective_args)
    binding_values = {
        "tool_name": tool_name,
        "effective_args": effective_snapshot,
        "task_id": task_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "tool_call_id": tool_call_id,
        "effective_cwd": prepared_runtime.effective_cwd,
        "effective_cwd_source": prepared_runtime.effective_cwd_source,
        "effective_cwd_authoritative": prepared_runtime.effective_cwd_authoritative,
    }
    return ToolDispatchPolicyInput(
        original_args=original_snapshot,
        policy_binding=compute_policy_binding(binding_values),
        **binding_values,
    )


def _is_policy_binding(value: object) -> bool:
    if type(value) is not str or len(value) != 64 or value != value.lower():
        return False
    return all(character in "0123456789abcdef" for character in value)


def _decision(
    is_allowed: bool,
    policy_code: str,
    message: str,
) -> PolicyDecision:
    return PolicyDecision(
        is_allowed=is_allowed,
        policy_code=policy_code,
        message=_truncate_utf8(message),
    )


def parse_policy_decision(
    decision: object,
    expected_binding: str,
) -> PolicyDecision:
    if not _is_policy_binding(expected_binding):
        return _decision(
            False,
            PolicyDecisionCode.BINDING_MISMATCH,
            "Policy binding did not match.",
        )
    if not isinstance(decision, Mapping):
        return _decision(False, PolicyDecisionCode.MALFORMED, "Malformed policy decision.")
    try:
        normalized = dict(decision)
    except Exception:
        return _decision(False, PolicyDecisionCode.MALFORMED, "Malformed policy decision.")

    action = normalized.get("action")
    if type(action) is not str:
        return _decision(False, PolicyDecisionCode.MALFORMED, "Malformed policy decision.")

    if action == "allow":
        if set(normalized) == {"action"}:
            return _decision(
                False,
                PolicyDecisionCode.BINDING_MISSING,
                "Policy binding is required.",
            )
        if set(normalized) != {"action", "policy_binding"}:
            return _decision(False, PolicyDecisionCode.MALFORMED, "Malformed policy decision.")
        binding = normalized.get("policy_binding")
        if type(binding) is not str or not hmac.compare_digest(binding, expected_binding):
            return _decision(
                False,
                PolicyDecisionCode.BINDING_MISMATCH,
                "Policy binding did not match.",
            )
        return _decision(True, PolicyDecisionCode.ALLOWED, "")

    if action == "block":
        if "message" not in normalized:
            return _decision(
                False,
                PolicyDecisionCode.EMPTY_BLOCK_MESSAGE,
                "A block message is required.",
            )
        if set(normalized) != {"action", "message"}:
            return _decision(False, PolicyDecisionCode.MALFORMED, "Malformed policy decision.")
        message = normalized.get("message")
        if type(message) is not str:
            return _decision(
                False,
                PolicyDecisionCode.INVALID_BLOCK_MESSAGE,
                "The block message must be a string.",
            )
        if not message.strip():
            return _decision(
                False,
                PolicyDecisionCode.EMPTY_BLOCK_MESSAGE,
                "A block message is required.",
            )
        return _decision(False, PolicyDecisionCode.BLOCKED, message.strip())

    return _decision(
        False,
        PolicyDecisionCode.NON_EXPLICIT,
        "Policy did not explicitly allow execution.",
    )


# ---------------------------------------------------------------------------
# Process-wide executor, semaphore, and quarantine
# ---------------------------------------------------------------------------

_REQUIRED_POLICY_MAX_WORKERS = 4
_required_policy_executor = DaemonThreadPoolExecutor(
    max_workers=_REQUIRED_POLICY_MAX_WORKERS,
    thread_name_prefix="required-policy",
)
_required_policy_slots = threading.BoundedSemaphore(_REQUIRED_POLICY_MAX_WORKERS)
_required_policy_quarantine: set[tuple[str, str, str]] = set()
_required_policy_quarantine_lock = threading.Lock()


def _required_policy_key(
    session_id: str,
    plugin_key: str,
    policy_name: str,
) -> tuple[str, str, str]:
    return session_id, plugin_key, policy_name


def _quarantine_required_policy(key: tuple[str, str, str]) -> None:
    with _required_policy_quarantine_lock:
        _required_policy_quarantine.add(key)


def _required_policy_block(
    registration: ToolPolicyRegistration,
    policy_code: str,
    message: str,
) -> ToolPolicyBlock:
    return ToolPolicyBlock(
        policy=registration.policy_name,
        policy_code=policy_code,
        message=message,
    )


def is_required_policy_quarantined(
    session_id: str,
    plugin_key: str,
    policy_name: str,
) -> bool:
    key = _required_policy_key(session_id, plugin_key, policy_name)
    with _required_policy_quarantine_lock:
        return key in _required_policy_quarantine


def clear_required_policy_quarantine(session_id: str | None = None) -> None:
    """Clear quarantine entries. When ``session_id`` is ``None`` clear all."""
    with _required_policy_quarantine_lock:
        if session_id is None:
            _required_policy_quarantine.clear()
        else:
            retained = {
                key
                for key in _required_policy_quarantine
                if key[0] != session_id
            }
            _required_policy_quarantine.clear()
            _required_policy_quarantine.update(retained)


def run_required_policy(
    registration: ToolPolicyRegistration,
    policy_input: ToolDispatchPolicyInput,
) -> ToolPolicyBlock | None:
    """Run one required policy callback and return the resulting block or ``None`` on explicit allow.

    A callback that times out or raises is quarantined so subsequent calls from
    the same (session, plugin, policy) triple are short-circuited without
    submitting another future. The matching capacity slot is released only
    from the worker's ``finally`` block, so wedged workers cannot create
    unbounded queued work.

    ``None`` is returned only when the callback produced a valid explicit allow.
    """
    quarantine_key = _required_policy_key(
        policy_input.session_id,
        registration.plugin_key,
        registration.policy_name,
    )
    if is_required_policy_quarantined(*quarantine_key):
        return _required_policy_block(
            registration,
            RequiredPolicyFailureCode.QUARANTINED,
            message="Required policy was quarantined from a prior failure.",
        )

    if not _required_policy_slots.acquire(blocking=False):
        return _required_policy_block(
            registration,
            RequiredPolicyFailureCode.EXECUTOR_SATURATED,
            message="Required policy executor is saturated.",
        )

    def _run_and_release() -> object:
        try:
            return registration.callback(policy_input.to_callback_payload())
        finally:
            _required_policy_slots.release()

    try:
        policy_context = copy_context()
        future = _required_policy_executor.submit(
            policy_context.run,
            _run_and_release,
        )
    except Exception:
        _required_policy_slots.release()
        _quarantine_required_policy(quarantine_key)
        return _required_policy_block(
            registration,
            RequiredPolicyFailureCode.CALLBACK_ERROR,
            message="Required policy callback could not be scheduled.",
        )

    try:
        decision = future.result(timeout=registration.timeout_ms / 1000.0)
    except FuturesTimeoutError:
        _quarantine_required_policy(quarantine_key)
        if future.done():
            try:
                completed_exception = future.exception(timeout=0)
            except Exception:
                completed_exception = FuturesTimeoutError()
            if completed_exception is not None:
                return _required_policy_block(
                    registration,
                    RequiredPolicyFailureCode.CALLBACK_ERROR,
                    message="Required policy callback failed.",
                )
        return _required_policy_block(
            registration,
            RequiredPolicyFailureCode.TIMEOUT,
            message="Required policy callback timed out.",
        )
    except Exception:
        _quarantine_required_policy(quarantine_key)
        return _required_policy_block(
            registration,
            RequiredPolicyFailureCode.CALLBACK_ERROR,
            message="Required policy callback failed.",
        )

    parsed = parse_policy_decision(decision, policy_input.policy_binding)
    if parsed.allows():
        return None

    if parsed.policy_code != PolicyDecisionCode.BLOCKED:
        _quarantine_required_policy(quarantine_key)
    return _required_policy_block(
        registration,
        parsed.policy_code,
        parsed.message,
    )


__all__ = [
    "JSONValue",
    "MAX_POLICY_BLOCK_MESSAGE_BYTES",
    "POLICY_SCHEMA_VERSION",
    "PolicyDecision",
    "PolicyDecisionCode",
    "PluginMiddlewareRegistration",
    "PreparedToolRuntime",
    "ToolDispatchPolicyInput",
    "ToolPolicyBlock",
    "ToolPolicyRegistration",
    "TOOL_DISPATCH_CONFORMANCE_TOOL_NAME",
    "compute_policy_binding",
    "create_tool_dispatch_policy_input",
    "parse_policy_decision",
]
