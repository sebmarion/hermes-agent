"""Required tool-dispatch policy contract.

Provider-neutral, fail-closed policy boundary that sees the final tool
arguments and the exact runtime working directory immediately before ordinary
tool execution.

The dispatch input contains only JSON-shaped tool and identity/cwd fields:
  * tool_name: str
  * effective_args: dict
  * task_id: str
  * session_id: str
  * turn_id: str
  * tool_call_id: str
  * effective_cwd: str | None
  * effective_cwd_source: str
  * effective_cwd_authoritative: bool

Never store prompts, source bytes, environment variables, command output,
credentials, or arbitrary runtime objects in the payload.

The policy binding is SHA-256 over canonical UTF-8 JSON containing:
  * schema_version: str
  * tool_name: str
  * effective_args: dict
  * task_id, session_id, turn_id, tool_call_id: str
  * effective_cwd: str | None
  * effective_cwd_source: str
  * effective_cwd_authoritative: bool

A valid allow response is exactly a mapping with ``action: "allow"`` and the
same ``policy_binding``. A valid block response is ``action: "block"`` with a
bounded non-empty message (capped at 1000 UTF-8 bytes). Missing plugin/
registration, load error, callback exception, timeout, executor saturation,
malformed response, binding mismatch, or any non-explicit action blocks before
the handler runs.
"""

import hashlib
import json
from typing import Any

# ---------------------------------------------------------------------------
# Stable internal policy codes
# ---------------------------------------------------------------------------

class PolicyDecisionCode:
    """Stable internal codes for policy decisions. Never leak raw exception
    text to the model; use these codes instead."""

    ALLOWED = "ALLOWED"
    BLOCKED = "BLOCKED"
    MALFORMED = "MALFORMED"
    NON_EXPLICIT = "NON_EXPLICIT"
    BINDING_MISMATCH = "BINDING_MISMATCH"
    EMPTY_BLOCK_MESSAGE = "EMPTY_BLOCK_MESSAGE"
    ALLOW_WITHOUT_BINDING = "ALLOW_WITHOUT_BINDING"


class PolicyDecision:
    """Result of parsing a policy decision."""

    def __init__(self, allows: bool, policy_code: str, message: str = "") -> None:
        self._allows = allows
        self._policy_code = policy_code
        self._message = message

    def allows(self) -> bool:
        return self._allows

    @property
    def policy_code(self) -> str:
        return self._policy_code

    @property
    def message(self) -> str:
        return self._message


# ---------------------------------------------------------------------------
# Immutable dataclasses
# ---------------------------------------------------------------------------

class ImmutableDataclass:
    """Mixin that prevents attribute mutation after construction."""

    def __setattr__(self, name: str, value: Any) -> None:
        if hasattr(self, name):
            raise AssertionError(
                f"Cannot modify immutable attribute '{name}' on {self.__class__.__name__}"
            )
        object.__setattr__(self, name, value)


from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ToolDispatchPolicyInput:
    """Immutable input to a required tool-dispatch policy callback.

    Contains only JSON-shaped tool and identity/cwd fields.
    """

    tool_name: str
    effective_args: dict
    task_id: str
    session_id: str
    turn_id: str
    tool_call_id: str
    effective_cwd: Optional[str]
    effective_cwd_source: str
    effective_cwd_authoritative: bool


@dataclass(frozen=True)
class PreparedToolRuntime:
    """Immutable prepared runtime context for tool execution.

    Contains only the authoritative working directory and its metadata.
    """

    effective_cwd: str
    effective_cwd_source: str
    effective_cwd_authoritative: bool


@dataclass(frozen=True)
class ToolPolicyBlock:
    """Immutable structured block result returned when a policy blocks execution."""

    error_type: str
    policy: str
    policy_code: str


@dataclass(frozen=True)
class ToolPolicyRegistration:
    """Immutable registration record for a plugin's required policy."""

    plugin_key: str
    policy_name: str
    callback: Any  # Callable
    timeout_ms: int


# ---------------------------------------------------------------------------
# compute_policy_binding
# ---------------------------------------------------------------------------

def compute_policy_binding(payload: dict) -> str:
    """Compute the policy binding as SHA-256 over canonical UTF-8 JSON.

    Args:
        payload: Dict containing the canonical fields:
            - tool_name: str
            - effective_args: dict
            - task_id: str
            - session_id: str
            - turn_id: str
            - tool_call_id: str
            - effective_cwd: str | None
            - effective_cwd_source: str
            - effective_cwd_authoritative: bool

    Returns:
        SHA-256 hex digest (64-character string).

    Raises:
        TypeError: If any field has a non-JSON-compatible type.
    """
    # Validate all fields are JSON-compatible before hashing.
    required_fields = {
        "tool_name": str,
        "effective_args": dict,
        "task_id": str,
        "session_id": str,
        "turn_id": str,
        "tool_call_id": str,
        "effective_cwd": (str, type(None)),
        "effective_cwd_source": str,
        "effective_cwd_authoritative": bool,
    }

    for field_name, expected_types in required_fields.items():
        value = payload.get(field_name)
        if value is None:
            raise TypeError(f"Missing required field: {field_name}")
        if not isinstance(value, expected_types):
            raise TypeError(
                f"Field '{field_name}' must be {expected_types}, "
                f"got {type(value).__name__}"
            )

    # Build canonical JSON with sorted keys for determinism.
    canonical_json = json.dumps(
        {
            "tool_name": payload["tool_name"],
            "effective_args": payload["effective_args"],
            "task_id": payload["task_id"],
            "session_id": payload["session_id"],
            "turn_id": payload["turn_id"],
            "tool_call_id": payload["tool_call_id"],
            "effective_cwd": payload["effective_cwd"],
            "effective_cwd_source": payload["effective_cwd_source"],
            "effective_cwd_authoritative": payload["effective_cwd_authoritative"],
        },
        sort_keys=True,
        ensure_ascii=False,
    )

    # Compute SHA-256 hex digest.
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# parse_policy_decision
# ---------------------------------------------------------------------------

def parse_policy_decision(decision: Any, expected_binding: str) -> PolicyDecision:
    """Parse and validate a policy decision.

    Args:
        decision: The policy decision to parse (typically a dict from a callback).
        expected_binding: The expected policy binding this decision must match.

    Returns:
        PolicyDecision with the parsed result.
    """
    # Validate decision structure.
    if not isinstance(decision, dict):
        return PolicyDecision(
            allows=False,
            policy_code=PolicyDecisionCode.MALFORMED,
            message="Decision is not a dict",
        )

    action = decision.get("action")

    if action is None:
        return PolicyDecision(
            allows=False,
            policy_code=PolicyDecisionCode.MALFORMED,
            message="Missing action field",
        )

    # Handle explicit allow.
    if action == "allow":
        policy_binding = decision.get("policy_binding")
        if policy_binding != expected_binding:
            return PolicyDecision(
                allows=False,
                policy_code=PolicyDecisionCode.BINDING_MISMATCH,
                message="Allow decision binding mismatch",
            )
        return PolicyDecision(
            allows=True,
            policy_code=PolicyDecisionCode.ALLOWED,
            message="Allowed",
        )

    # Handle explicit block.
    if action == "block":
        message = decision.get("message", "")
        if not message:
            return PolicyDecision(
                allows=False,
                policy_code=PolicyDecisionCode.EMPTY_BLOCK_MESSAGE,
                message="Block decision missing message",
            )
        # Cap message at 1000 UTF-8 bytes.
        if len(message.encode("utf-8")) > 1000:
            message = message.encode("utf-8")[:1000].decode("utf-8", errors="ignore")
        return PolicyDecision(
            allows=False,
            policy_code=PolicyDecisionCode.BLOCKED,
            message=message,
        )

    # Non-explicit actions are treated as blocks.
    return PolicyDecision(
        allows=False,
        policy_code=PolicyDecisionCode.NON_EXPLICIT,
        message=f"Non-explicit action: {action}",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "ImmutableDataclass",
    "PolicyDecision",
    "PolicyDecisionCode",
    "PreparedToolRuntime",
    "ToolDispatchPolicyInput",
    "ToolPolicyBlock",
    "ToolPolicyRegistration",
    "compute_policy_binding",
    "parse_policy_decision",
]
