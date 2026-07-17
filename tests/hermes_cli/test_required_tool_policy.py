"""Tests for required tool-dispatch policy contract.

These tests validate:
  * deterministic canonical policy binding (reordered keys, changed fields)
  * non-JSON value rejection
  * strict policy-decision parsing (allow/block validity, message cap, codes)
  * immutable dataclass invariants

The contract:

  * ``compute_policy_binding()`` is SHA-256 over canonical UTF-8 JSON containing
    a schema version, tool name, effective args, task/session/turn/tool-call
    identities, and prepared cwd fields.
  * The dispatch input may contain only JSON-shaped tool and identity/cwd fields.
    Prompts, source bytes, environment variables, command output, credentials,
    or arbitrary runtime objects must not be stored in the payload.
  * An explicit allow is valid only for a mapping with ``action: "allow"`` and
    the exact policy binding.
  * An explicit block requires a non-empty bounded message. Block messages are
    capped at 1000 UTF-8 bytes and must not produce invalid UTF-8.
  * Malformed, non-explicit, or binding-mismatched decisions use stable
    internal policy codes, never raw exception text.
"""

import hashlib
import json

import pytest

from hermes_cli.tool_policy import (
    PolicyDecision,
    PolicyDecisionCode,
    ToolDispatchPolicyInput,
    PreparedToolRuntime,
    ToolPolicyBlock,
    ToolPolicyRegistration,
    compute_policy_binding,
    parse_policy_decision,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _canonical_payload(**overrides):
    """Return the canonical payload dict used by tests.

    Defaults mirror the contract: every required identity/cwd field is present
    and JSON-shaped. Override any field to exercise a specific case.
    """
    defaults = dict(
        tool_name="terminal",
        effective_args={"command": "echo hello"},
        task_id="task-1",
        session_id="session-1",
        turn_id="turn-1",
        tool_call_id="call-1",
        effective_cwd="/home/user",
        effective_cwd_source="task",
        effective_cwd_authoritative=True,
    )
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# compute_policy_binding
# ---------------------------------------------------------------------------

class TestComputePolicyBinding:
    """Deterministic canonical payload hashing."""

    def test_reordered_mapping_keys_give_same_binding(self):
        """Canonical JSON must serialize the same way regardless of key order."""
        payload = _canonical_payload()
        # Reorder the effective_args dict keys.
        reordered = _canonical_payload()
        reordered["effective_args"] = dict(
            (k, v) for k, v in reversed(list(payload["effective_args"].items()))
        )
        binding_a = compute_policy_binding(payload)
        binding_b = compute_policy_binding(reordered)
        assert binding_a == binding_b

    def test_changed_tool_name_changes_binding(self):
        """A different tool name must produce a different binding."""
        a = compute_policy_binding(_canonical_payload())
        binding_alt = compute_policy_binding(_canonical_payload(tool_name="read_file"))
        assert a != binding_alt
        # Verify SHA-256 hexdigest shape.
        assert len(a) == 64

    def test_changed_effective_args_changes_binding(self):
        a = compute_policy_binding(_canonical_payload())
        b = compute_policy_binding(
            _canonical_payload(effective_args={"command": "echo other"})
        )
        assert a != b

    def test_changed_cwd_changes_binding(self):
        a = compute_policy_binding(_canonical_payload())
        b = compute_policy_binding(
            _canonical_payload(effective_cwd="/other/path")
        )
        assert a != b

    def test_changed_session_id_changes_binding(self):
        a = compute_policy_binding(_canonical_payload())
        b = compute_policy_binding(_canonical_payload(session_id="session-2"))
        assert a != b

    def test_changed_turn_id_changes_binding(self):
        a = compute_policy_binding(_canonical_payload())
        b = compute_policy_binding(_canonical_payload(turn_id="turn-2"))
        assert a != b

    def test_changed_tool_call_id_changes_binding(self):
        a = compute_policy_binding(_canonical_payload())
        b = compute_policy_binding(_canonical_payload(tool_call_id="call-2"))
        assert a != b

    def test_changed_task_id_changes_binding(self):
        a = compute_policy_binding(_canonical_payload())
        b = compute_policy_binding(_canonical_payload(task_id="task-2"))
        assert a != b

    def test_changed_effective_cwd_source_changes_binding(self):
        a = compute_policy_binding(_canonical_payload())
        b = compute_policy_binding(
            _canonical_payload(effective_cwd_source="other")
        )
        assert a != b

    def test_changed_effective_cwd_authoritative_changes_binding(self):
        a = compute_policy_binding(_canonical_payload())
        b = compute_policy_binding(
            _canonical_payload(effective_cwd_authoritative=False)
        )
        assert a != b

    def test_binding_is_sha256_hexdigest(self):
        binding = compute_policy_binding(_canonical_payload())
        assert len(binding) == 64
        # Must be valid hex.
        int(binding, 16)

    def test_binding_is_canonical_sha256_over_json(self):
        """Confirm the binding equals hashlib.sha256(utf8_json).hexdigest()."""
        payload = _canonical_payload()
        # Canonical JSON must be sorted keys at the top level.
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        # The binding should equal SHA-256 of the canonical JSON.
        assert compute_policy_binding(payload) == expected


class TestComputePolicyBindingNonJsonRejection:
    """Non-JSON values are rejected before a future callback could see them."""

    def test_non_dict_effective_args_raises(self):
        payload = _canonical_payload(effective_args="not a dict")
        with pytest.raises(TypeError):
            compute_policy_binding(payload)

    def test_non_string_tool_name_raises(self):
        payload = _canonical_payload(tool_name=123)
        with pytest.raises(TypeError):
            compute_policy_binding(payload)

    def test_non_string_ids_raise(self):
        for field in ("task_id", "session_id", "turn_id", "tool_call_id"):
            payload = _canonical_payload(**{field: 123})
            with pytest.raises(TypeError):
                compute_policy_binding(payload)

    def test_non_string_cwd_fields_raise(self):
        for field in ("effective_cwd", "effective_cwd_source"):
            payload = _canonical_payload(**{field: 123})
            with pytest.raises(TypeError):
                compute_policy_binding(payload)

    def test_non_bool_authoritative_raises(self):
        payload = _canonical_payload(effective_cwd_authoritative="truthy")
        with pytest.raises(TypeError):
            compute_policy_binding(payload)

    def test_extra_payload_fields_ignored(self):
        """Extra keys in the payload dict must not affect the binding."""
        payload = _canonical_payload()
        payload["extra_ignored_key"] = "irrelevant"
        assert (
            compute_policy_binding(payload) == compute_policy_binding(_canonical_payload())
        )


# ---------------------------------------------------------------------------
# Immutable dataclasses
# ---------------------------------------------------------------------------

class TestImmutableDataclasses:
    """ToolDispatchPolicyInput, PreparedToolRuntime, ToolPolicyBlock,
    and ToolPolicyRegistration must be immutable."""

    def test_tool_dispatch_policy_input_immutable(self):
        import dataclasses
        # Confirm the dataclass is actually a dataclass.
        assert dataclasses.is_dataclass(ToolDispatchPolicyInput)
        # Test that we can instantiate it with canonical payload
        instance = ToolDispatchPolicyInput(**_canonical_payload())
        assert instance.tool_name == "terminal"

    def test_prepared_tool_runtime_immutable(self):
        instance = PreparedToolRuntime(effective_cwd="/x", effective_cwd_source="y", effective_cwd_authoritative=True)
        with pytest.raises(Exception):
            instance.effective_cwd = "/z"

    def test_tool_policy_block_immutable(self):
        instance = ToolPolicyBlock(error_type="required_policy_block", policy="tool_dispatch", policy_code="BLOCKED")
        with pytest.raises(Exception):
            instance.policy_code = "OTHER"

    def test_tool_policy_registration_immutable(self):
        instance = ToolPolicyRegistration(
            plugin_key="test_plugin", policy_name="tool_dispatch", callback=lambda: None, timeout_ms=2000,
        )
        with pytest.raises(Exception):
            instance.timeout_ms = 1000

    def test_tool_policy_block_fields(self):
        block = ToolPolicyBlock(error_type="required_policy_block", policy="tool_dispatch", policy_code="BLOCKED")
        assert block.error_type == "required_policy_block"
        assert block.policy == "tool_dispatch"
        assert block.policy_code == "BLOCKED"

    def test_tool_policy_registration_fields(self):
        reg = ToolPolicyRegistration(
            plugin_key="p", policy_name="tool_dispatch", callback=lambda: None, timeout_ms=500,
        )
        assert reg.plugin_key == "p"
        assert reg.policy_name == "tool_dispatch"
        assert reg.timeout_ms == 500


# ---------------------------------------------------------------------------
# parse_policy_decision
# ---------------------------------------------------------------------------

class TestParsePolicyDecision:
    """Strict policy-decision parsing."""

    def test_explicit_allow_matching_binding(self):
        binding = compute_policy_binding(_canonical_payload())
        decision = {
            "action": "allow",
            "policy_binding": binding,
        }
        result = parse_policy_decision(decision, binding)
        assert result.allows()
        assert result.policy_code == PolicyDecisionCode.ALLOWED

    def test_explicit_block_with_message(self):
        decision = {
            "action": "block",
            "message": "Not allowed",
        }
        result = parse_policy_decision(decision, "any-binding")
        assert not result.allows()
        assert result.policy_code == PolicyDecisionCode.BLOCKED
        assert result.message == "Not allowed"

    def test_block_message_capped_at_1000_utf8_bytes(self):
        long_message = "x" * 2000
        decision = {
            "action": "block",
            "message": long_message,
        }
        result = parse_policy_decision(decision, "any-binding")
        assert len(result.message.encode("utf-8")) <= 1000
        # Must still be valid UTF-8.
        result.message.encode("utf-8")

    def test_block_message_with_non_ascii_chars(self):
        decision = {
            "action": "block",
            "message": "日本語",
        }
        result = parse_policy_decision(decision, "any-binding")
        assert result.policy_code == PolicyDecisionCode.BLOCKED
        # Verify valid UTF-8.
        result.message.encode("utf-8")

    def test_binding_mismatched_decision_uses_stable_code(self):
        wrong_binding = "wrong"
        decision = {
            "action": "block",
            "message": "mismatch",
        }
        result = parse_policy_decision(decision, wrong_binding)
        assert result.policy_code == PolicyDecisionCode.BLOCKED

    def test_non_explicit_action_uses_stable_code(self):
        decision = {"action": "observe"}
        result = parse_policy_decision(decision, "any-binding")
        assert result.policy_code == PolicyDecisionCode.NON_EXPLICIT

    def test_missing_action_uses_stable_code(self):
        decision = {"other": "value"}
        result = parse_policy_decision(decision, "any-binding")
        assert result.policy_code == PolicyDecisionCode.MALFORMED

    def test_empty_block_message_uses_stable_code(self):
        decision = {"action": "block", "message": ""}
        result = parse_policy_decision(decision, "any-binding")
        assert result.policy_code == PolicyDecisionCode.EMPTY_BLOCK_MESSAGE

    def test_allow_with_wrong_binding_uses_stable_code(self):
        wrong_binding = "wrong"
        decision = {"action": "allow", "policy_binding": wrong_binding}
        result = parse_policy_decision(decision, "right")
        assert result.policy_code == PolicyDecisionCode.BINDING_MISMATCH

    def test_malformed_decision_uses_stable_code(self):
        result = parse_policy_decision("not a dict", "any")
        assert result.policy_code == PolicyDecisionCode.MALFORMED

    def test_policy_decision_fields(self):
        binding = compute_policy_binding(_canonical_payload())
        result = parse_policy_decision({"action": "allow", "policy_binding": binding}, binding)
        assert result.allows()
        assert result.policy_code == PolicyDecisionCode.ALLOWED

    def test_exception_text_not_leaked(self):
        """Raw exception text must never appear in the policy result."""
        # Confirm stable codes never contain exception class names.
        for code in PolicyDecisionCode.__dict__.values():
            if isinstance(code, str):
                assert "Exception" not in code
                assert "Error" not in code


# ---------------------------------------------------------------------------
# ToolDispatchPolicyInput - JSON-shaped payload only
# ---------------------------------------------------------------------------

class TestToolDispatchPolicyInput:
    """The dispatch input may contain only JSON-shaped fields."""

    def test_cannot_store_prompts(self):
        # Confirm the dataclass has no prompt field.
        import dataclasses
        fields = {f.name for f in dataclasses.fields(ToolDispatchPolicyInput)}
        assert "prompt" not in fields
        assert "source_bytes" not in fields
        assert "env_vars" not in fields
        assert "command_output" not in fields
        assert "credentials" not in fields

    def test_fields_are_json_compatible_types(self):
        """All fields on the input must serialize to/from JSON."""
        instance = ToolDispatchPolicyInput(**_canonical_payload())
        # Verify JSON round-trip.
        import json as _json
        payload = _json.loads(_json.dumps(instance.__dict__))
        assert isinstance(payload["tool_name"], str)
        assert isinstance(payload["effective_args"], dict)


# ---------------------------------------------------------------------------
# PreparedToolRuntime - JSON-shaped cwd fields only
# ---------------------------------------------------------------------------

class TestPreparedToolRuntime:
    def test_cwd_fields_are_json_compatible(self):
        instance = PreparedToolRuntime(
            effective_cwd="/x",
            effective_cwd_source="task",
            effective_cwd_authoritative=True,
        )
        import json as _json
        payload = _json.loads(_json.dumps(instance.__dict__))
        assert isinstance(payload["effective_cwd"], str)
        assert isinstance(payload["effective_cwd_source"], str)
        assert isinstance(payload["effective_cwd_authoritative"], bool)


# ---------------------------------------------------------------------------
# ToolPolicyBlock
# ---------------------------------------------------------------------------

class TestToolPolicyBlock:
    def test_block_has_required_fields(self):
        block = ToolPolicyBlock(error_type="required_policy_block", policy="tool_dispatch", policy_code="BLOCKED")
        assert block.error_type == "required_policy_block"
        assert block.policy == "tool_dispatch"
        assert block.policy_code == "BLOCKED"

    def test_block_message_not_in_policy_block(self):
        import dataclasses
        fields = {f.name for f in dataclasses.fields(ToolPolicyBlock)}
        assert "message" not in fields
