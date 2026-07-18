import hashlib
import json
from dataclasses import FrozenInstanceError, fields
from types import MappingProxyType

import pytest

from hermes_cli.tool_policy import (
    MAX_POLICY_BLOCK_MESSAGE_BYTES,
    POLICY_SCHEMA_VERSION,
    PolicyDecision,
    PolicyDecisionCode,
    PreparedToolRuntime,
    ToolDispatchPolicyInput,
    ToolPolicyBlock,
    ToolPolicyRegistration,
    compute_policy_binding,
    create_tool_dispatch_policy_input,
    parse_policy_decision,
)


def _runtime(cwd: str | None = "/workspace") -> PreparedToolRuntime:
    return PreparedToolRuntime(
        effective_cwd=cwd,
        effective_cwd_source="process_cwd",
        effective_cwd_authoritative=True,
    )


def _binding_fields(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "tool_name": "terminal",
        "effective_args": {
            "z": 3,
            "a": {"second": [1, True, None], "first": "value"},
            "m": 2.5,
        },
        "task_id": "task-1",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "tool_call_id": "call-1",
        "effective_cwd": "/workspace",
        "effective_cwd_source": "process_cwd",
        "effective_cwd_authoritative": True,
    }
    values.update(overrides)
    return values


def _create_input(
    *,
    original_args: dict | None = None,
    effective_args: dict | None = None,
    cwd: str | None = "/workspace",
) -> ToolDispatchPolicyInput:
    return create_tool_dispatch_policy_input(
        tool_name="terminal",
        original_args=original_args or {"command": "before"},
        effective_args=effective_args or {"command": "after"},
        task_id="task-1",
        session_id="session-1",
        turn_id="turn-1",
        tool_call_id="call-1",
        prepared_runtime=_runtime(cwd),
    )


class TestPolicyBinding:
    def test_hashes_exact_canonical_payload_with_schema_version(self):
        values = _binding_fields()
        canonical = json.dumps(
            {"schema_version": POLICY_SCHEMA_VERSION, **values},
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        assert compute_policy_binding(values) == hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()

    def test_reordered_top_level_and_nested_keys_have_same_binding(self):
        first = _binding_fields()
        second = {
            "effective_cwd_authoritative": True,
            "effective_cwd_source": "process_cwd",
            "effective_cwd": "/workspace",
            "tool_call_id": "call-1",
            "turn_id": "turn-1",
            "session_id": "session-1",
            "task_id": "task-1",
            "effective_args": {
                "m": 2.5,
                "a": {"first": "value", "second": [1, True, None]},
                "z": 3,
            },
            "tool_name": "terminal",
        }
        assert compute_policy_binding(first) == compute_policy_binding(second)

    @pytest.mark.parametrize(
        ("field_name", "changed"),
        [
            ("tool_name", "read_file"),
            ("effective_args", {"command": "different"}),
            ("task_id", "task-2"),
            ("session_id", "session-2"),
            ("turn_id", "turn-2"),
            ("tool_call_id", "call-2"),
            ("effective_cwd", "/different"),
            ("effective_cwd_source", "explicit_workdir"),
            ("effective_cwd_authoritative", False),
        ],
    )
    def test_each_authorization_field_changes_binding(self, field_name, changed):
        assert compute_policy_binding(_binding_fields()) != compute_policy_binding(
            _binding_fields(**{field_name: changed})
        )

    def test_nullable_cwd_is_valid_and_deterministic(self):
        first = compute_policy_binding(_binding_fields(effective_cwd=None))
        second = compute_policy_binding(_binding_fields(effective_cwd=None))
        assert first == second
        assert first != compute_policy_binding(_binding_fields())

    def test_requires_exact_binding_field_set(self):
        missing = _binding_fields()
        missing.pop("turn_id")
        with pytest.raises(TypeError):
            compute_policy_binding(missing)
        with pytest.raises(TypeError):
            compute_policy_binding({**_binding_fields(), "prompt": "secret"})

    def test_accepts_input_object(self):
        policy_input = _create_input()
        assert compute_policy_binding(policy_input) == policy_input.policy_binding


class TestJsonAndFieldValidation:
    @pytest.mark.parametrize(
        "invalid",
        [
            b"bytes",
            {"a", "set"},
            ("tuple",),
            object(),
            complex(1, 2),
            float("nan"),
            float("inf"),
            float("-inf"),
            {1: "non-string key"},
            {"nested": ["ok", {"bad": b"bytes"}]},
        ],
    )
    def test_rejects_nested_non_json_effective_args(self, invalid):
        with pytest.raises(TypeError):
            compute_policy_binding(_binding_fields(effective_args={"value": invalid}))

    def test_accepts_json_numbers(self):
        binding = compute_policy_binding(
            _binding_fields(effective_args={"integer": 3, "float": 1.25})
        )
        assert len(binding) == 64

    @pytest.mark.parametrize(
        ("field_name", "invalid"),
        [
            ("tool_name", 1),
            ("effective_args", "not-an-object"),
            ("task_id", 1),
            ("session_id", None),
            ("turn_id", []),
            ("tool_call_id", {}),
            ("effective_cwd", 1),
            ("effective_cwd_source", False),
            ("effective_cwd_authoritative", "yes"),
        ],
    )
    def test_rejects_wrong_binding_field_types(self, field_name, invalid):
        with pytest.raises(TypeError):
            compute_policy_binding(_binding_fields(**{field_name: invalid}))

    def test_factory_rejects_nested_non_json_original_args(self):
        with pytest.raises(TypeError):
            _create_input(original_args={"secret": b"bytes"})


class TestPolicyInputFactory:
    def test_populates_complete_callback_payload(self):
        policy_input = _create_input()
        payload = policy_input.to_callback_payload()
        assert set(payload) == {
            "tool_name",
            "original_args",
            "effective_args",
            "task_id",
            "session_id",
            "turn_id",
            "tool_call_id",
            "effective_cwd",
            "effective_cwd_source",
            "effective_cwd_authoritative",
            "policy_binding",
        }
        assert payload["policy_binding"] == policy_input.policy_binding

    def test_original_args_are_visible_but_not_hashed(self):
        first = _create_input(original_args={"command": "one"})
        second = _create_input(original_args={"command": "two"})
        assert first.original_args != second.original_args
        assert first.policy_binding == second.policy_binding

    def test_snapshots_argument_dicts(self):
        original = {"command": ["before"]}
        effective = {"command": ["after"]}
        policy_input = _create_input(
            original_args=original,
            effective_args=effective,
        )
        original["command"].append("mutated")
        effective["command"].append("mutated")
        assert policy_input.original_args == {"command": ["before"]}
        assert policy_input.effective_args == {"command": ["after"]}

    def test_callback_payload_returns_fresh_argument_copies(self):
        policy_input = _create_input()
        payload = policy_input.to_callback_payload()
        payload["effective_args"]["command"] = "mutated"
        assert policy_input.effective_args == {"command": "after"}

    def test_direct_constructor_rejects_wrong_binding(self):
        with pytest.raises(ValueError):
            ToolDispatchPolicyInput(
                tool_name="terminal",
                original_args={},
                effective_args={},
                task_id="task",
                session_id="session",
                turn_id="turn",
                tool_call_id="call",
                effective_cwd=None,
                effective_cwd_source="remote_unmapped",
                effective_cwd_authoritative=False,
                policy_binding="0" * 64,
            )

    def test_unknown_factory_keyword_is_rejected(self):
        kwargs = {
            "tool_name": "terminal",
            "original_args": {},
            "effective_args": {},
            "task_id": "task",
            "session_id": "session",
            "turn_id": "turn",
            "tool_call_id": "call",
            "prepared_runtime": _runtime(),
            "prompt": "secret",
        }
        with pytest.raises(TypeError):
            create_tool_dispatch_policy_input(**kwargs)

    def test_factory_requires_prepared_runtime(self):
        with pytest.raises(TypeError):
            create_tool_dispatch_policy_input(
                tool_name="terminal",
                original_args={},
                effective_args={},
                task_id="task",
                session_id="session",
                turn_id="turn",
                tool_call_id="call",
                prepared_runtime=object(),
            )

    def test_sensitive_fields_are_absent(self):
        field_names = {item.name for item in fields(ToolDispatchPolicyInput)}
        assert not field_names & {
            "prompt",
            "source_bytes",
            "environment",
            "command_output",
            "credentials",
        }


class TestImmutableContracts:
    @pytest.mark.parametrize(
        ("instance", "field_name", "value"),
        [
            (_create_input(), "tool_name", "other"),
            (_runtime(), "effective_cwd", "/other"),
            (
                ToolPolicyRegistration("plugin", "tool_dispatch", lambda _: {}, 2_000),
                "timeout_ms",
                1,
            ),
            (
                ToolPolicyBlock("tool_dispatch", "policy_blocked", "blocked"),
                "policy_code",
                "other",
            ),
        ],
    )
    def test_required_dataclasses_are_frozen(self, instance, field_name, value):
        with pytest.raises(FrozenInstanceError):
            setattr(instance, field_name, value)

    def test_policy_decision_is_frozen(self):
        decision = PolicyDecision(False, PolicyDecisionCode.MALFORMED, "fixed")
        with pytest.raises(FrozenInstanceError):
            decision.policy_code = "other"


class TestDecisionParsing:
    def test_explicit_allow_requires_matching_binding(self):
        binding = _create_input().policy_binding
        decision = parse_policy_decision(
            MappingProxyType({"action": "allow", "policy_binding": binding}),
            binding,
        )
        assert decision.allows()
        assert decision.policy_code == PolicyDecisionCode.ALLOWED
        assert decision.message == ""

    def test_allow_without_binding_is_stably_blocked(self):
        decision = parse_policy_decision({"action": "allow"}, "a" * 64)
        assert not decision.allows()
        assert decision.policy_code == PolicyDecisionCode.BINDING_MISSING

    def test_allow_with_wrong_binding_is_stably_blocked(self):
        decision = parse_policy_decision(
            {"action": "allow", "policy_binding": "b" * 64},
            "a" * 64,
        )
        assert decision.policy_code == PolicyDecisionCode.BINDING_MISMATCH

    def test_invalid_expected_binding_fails_closed(self):
        decision = parse_policy_decision(
            {"action": "allow", "policy_binding": "short"},
            "short",
        )
        assert not decision.allows()
        assert decision.policy_code == PolicyDecisionCode.BINDING_MISMATCH

    def test_allow_with_extra_fields_is_malformed(self):
        decision = parse_policy_decision(
            {"action": "allow", "policy_binding": "a" * 64, "extra": True},
            "a" * 64,
        )
        assert decision.policy_code == PolicyDecisionCode.MALFORMED

    def test_explicit_block_returns_bounded_safe_message(self):
        message = "一" * 400
        decision = parse_policy_decision(
            {"action": "block", "message": message},
            "a" * 64,
        )
        assert not decision.allows()
        assert decision.policy_code == PolicyDecisionCode.BLOCKED
        assert len(decision.message.encode("utf-8")) <= MAX_POLICY_BLOCK_MESSAGE_BYTES
        decision.message.encode("utf-8")

    def test_block_trims_before_utf8_bounding(self):
        decision = parse_policy_decision(
            {"action": "block", "message": " " * 1_100 + "reason"},
            "a" * 64,
        )
        assert decision.policy_code == PolicyDecisionCode.BLOCKED
        assert decision.message == "reason"

    @pytest.mark.parametrize("message", [None, 1, [], {}, b"bytes"])
    def test_non_string_block_messages_are_invalid(self, message):
        decision = parse_policy_decision(
            {"action": "block", "message": message},
            "a" * 64,
        )
        assert decision.policy_code == PolicyDecisionCode.INVALID_BLOCK_MESSAGE

    @pytest.mark.parametrize("message", ["", " ", "\n\t"])
    def test_empty_block_messages_are_invalid(self, message):
        decision = parse_policy_decision(
            {"action": "block", "message": message},
            "a" * 64,
        )
        assert decision.policy_code == PolicyDecisionCode.EMPTY_BLOCK_MESSAGE

    def test_missing_block_message_is_stably_blocked(self):
        decision = parse_policy_decision({"action": "block"}, "a" * 64)
        assert decision.policy_code == PolicyDecisionCode.EMPTY_BLOCK_MESSAGE

    def test_block_with_extra_fields_is_malformed(self):
        decision = parse_policy_decision(
            {"action": "block", "message": "no", "extra": True},
            "a" * 64,
        )
        assert decision.policy_code == PolicyDecisionCode.MALFORMED

    @pytest.mark.parametrize("malformed", [None, True, 1, "allow", [], object()])
    def test_non_mapping_decisions_are_malformed(self, malformed):
        decision = parse_policy_decision(malformed, "a" * 64)
        assert decision.policy_code == PolicyDecisionCode.MALFORMED

    def test_non_explicit_action_never_echoes_untrusted_content(self):
        sentinel = "do-not-echo-this-secret"
        decision = parse_policy_decision(
            {"action": sentinel},
            "a" * 64,
        )
        assert decision.policy_code == PolicyDecisionCode.NON_EXPLICIT
        assert sentinel not in decision.message

    def test_stable_codes_are_lowercase_and_bounded(self):
        codes = [
            value
            for name, value in vars(PolicyDecisionCode).items()
            if name.isupper()
        ]
        assert codes
        assert all(code == code.lower() and len(code) < 80 for code in codes)


class TestStructuredBlock:
    def test_has_stable_result_shape(self):
        block = ToolPolicyBlock(
            policy="tool_dispatch",
            policy_code=PolicyDecisionCode.BLOCKED,
            message="not authorized",
        )
        assert block.to_result() == {
            "status": "blocked",
            "error_type": "required_policy_block",
            "policy": "tool_dispatch",
            "policy_code": "policy_blocked",
            "message": "not authorized",
        }

    def test_bounds_multibyte_message_at_construction(self):
        block = ToolPolicyBlock(
            policy="tool_dispatch",
            policy_code=PolicyDecisionCode.BLOCKED,
            message="一" * 400,
        )
        assert len(block.message.encode("utf-8")) <= MAX_POLICY_BLOCK_MESSAGE_BYTES
        block.message.encode("utf-8")

    def test_trims_before_bounding_message_at_construction(self):
        block = ToolPolicyBlock(
            policy="tool_dispatch",
            policy_code=PolicyDecisionCode.BLOCKED,
            message=" " * 1_100 + "reason",
        )
        assert block.message == "reason"
