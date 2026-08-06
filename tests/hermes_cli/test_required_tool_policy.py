import hashlib
import json
import sys
import threading
from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from types import MappingProxyType
from unittest.mock import MagicMock, patch

import pytest
import yaml

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli import plugins as plugins_mod

from hermes_cli.plugins import (
    LoadedPlugin,
    PluginManager,
    PluginManifest,
    authorize_required_tool_policies,
)
from hermes_cli.tool_policy import (
    MAX_POLICY_BLOCK_MESSAGE_BYTES,
    POLICY_SCHEMA_VERSION,
    PolicyDecision,
    PolicyDecisionCode,
    PreparedToolRuntime,
    RequiredPolicyFailureCode,
    ToolDispatchPolicyInput,
    ToolPolicyBlock,
    ToolPolicyRegistration,
    clear_required_policy_quarantine,
    compute_policy_binding,
    create_tool_dispatch_policy_input,
    is_required_policy_quarantined,
    parse_policy_decision,
    run_required_policy,
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


def _runner_input(session_id: str = "session-runner") -> ToolDispatchPolicyInput:
    return create_tool_dispatch_policy_input(
        tool_name="terminal",
        original_args={"command": "before"},
        effective_args={"command": "after"},
        task_id="task-runner",
        session_id=session_id,
        turn_id="turn-runner",
        tool_call_id=f"call-{session_id}",
        prepared_runtime=_runtime(),
    )


def _registration(
    callback,
    *,
    plugin_key: str = "governor",
    timeout_ms: int = 250,
) -> ToolPolicyRegistration:
    return ToolPolicyRegistration(
        plugin_key=plugin_key,
        policy_name="tool_dispatch",
        callback=callback,
        timeout_ms=timeout_ms,
    )


def _allow(payload: dict) -> dict:
    return {
        "action": "allow",
        "policy_binding": payload["policy_binding"],
    }


def _assert_policy_slots_recovered() -> None:
    from hermes_cli import tool_policy

    acquired = 0
    try:
        for _ in range(tool_policy._REQUIRED_POLICY_MAX_WORKERS):
            assert tool_policy._required_policy_slots.acquire(timeout=1)
            acquired += 1
    finally:
        for _ in range(acquired):
            tool_policy._required_policy_slots.release()


@pytest.fixture(autouse=True)
def _reset_required_policy_runner_state():
    snapshot_before = plugins_mod._capture_required_policy_runtime_snapshot()
    private_modules_before = {
        name
        for name in sys.modules
        if name.startswith("_hermes_required_policy_recovery_")
    }
    clear_required_policy_quarantine()
    try:
        yield
    finally:
        clear_required_policy_quarantine()
        with plugins_mod._required_policy_snapshot_lock:
            plugins_mod._required_policy_runtime_snapshot = snapshot_before
        for module_name in tuple(sys.modules):
            if (
                module_name.startswith("_hermes_required_policy_recovery_")
                and module_name not in private_modules_before
            ):
                sys.modules.pop(module_name, None)
        _assert_policy_slots_recovered()


def _manager_with_plugin(
    *,
    plugin_key: str = "governor",
    enabled: bool,
    error: str | None = None,
    registration: ToolPolicyRegistration | None = None,
) -> PluginManager:
    manager = PluginManager()
    manager._discovered = True
    manager._discovery_home = plugins_mod._resolved_hermes_home()
    manager._plugins[plugin_key] = LoadedPlugin(
        manifest=PluginManifest(
            name=plugin_key,
            key=plugin_key,
            provides_policies=["tool_dispatch"],
        ),
        enabled=enabled,
        error=error,
    )
    if registration is not None:
        manager._register_policy(registration)
    return manager


class TestRequiredPolicyRunner:
    def test_uses_one_eager_process_wide_bounded_executor(self):
        from hermes_cli import tool_policy
        from tools.daemon_pool import DaemonThreadPoolExecutor

        assert isinstance(
            tool_policy._required_policy_executor,
            DaemonThreadPoolExecutor,
        )
        assert tool_policy._required_policy_executor._max_workers == 4
        assert tool_policy._REQUIRED_POLICY_MAX_WORKERS == 4
        assert tool_policy._required_policy_slots._initial_value == 4

    def test_explicit_allow_returns_none(self):
        assert run_required_policy(_registration(_allow), _runner_input()) is None

    def test_callback_inherits_dispatch_home_context(self, tmp_path, monkeypatch):
        context_home = tmp_path / "context-home"
        process_home = tmp_path / "process-home"
        observed_homes = []
        monkeypatch.setenv("HERMES_HOME", str(process_home))
        token = set_hermes_home_override(context_home)
        try:
            registration = _registration(
                lambda payload: observed_homes.append(
                    plugins_mod._resolved_hermes_home()
                )
                or _allow(payload)
            )

            assert run_required_policy(registration, _runner_input()) is None
            assert observed_homes == [str(context_home.resolve())]
        finally:
            reset_hermes_home_override(token)

    def test_explicit_block_is_bounded_and_not_quarantined(self):
        calls = []

        def callback(_payload):
            calls.append(True)
            return {"action": "block", "message": " denied "}

        registration = _registration(callback)
        policy_input = _runner_input()

        first = run_required_policy(registration, policy_input)
        second = run_required_policy(registration, policy_input)

        assert first is not None
        assert first.to_result() == {
            "status": "blocked",
            "error_type": "required_policy_block",
            "policy": "tool_dispatch",
            "policy_code": PolicyDecisionCode.BLOCKED,
            "message": "denied",
        }
        assert second is not None
        assert second.policy_code == PolicyDecisionCode.BLOCKED
        assert calls == [True, True]
        assert not is_required_policy_quarantined(
            policy_input.session_id,
            registration.plugin_key,
            registration.policy_name,
        )

    def test_callback_exception_is_safe_and_quarantines_without_resubmit(self):
        calls = []

        def callback(_payload):
            calls.append(True)
            raise RuntimeError("TOP_SECRET_CALLBACK_FAILURE")

        registration = _registration(callback)
        policy_input = _runner_input()

        first = run_required_policy(registration, policy_input)
        second = run_required_policy(registration, policy_input)

        assert first is not None
        assert first.policy == "tool_dispatch"
        assert first.policy_code == RequiredPolicyFailureCode.CALLBACK_ERROR
        assert "TOP_SECRET_CALLBACK_FAILURE" not in json.dumps(first.to_result())
        assert second is not None
        assert second.policy_code == RequiredPolicyFailureCode.QUARANTINED
        assert calls == [True]

    def test_callback_timeout_exception_is_classified_as_callback_error(self):
        def callback(_payload):
            raise FuturesTimeoutError("TOP_SECRET_CALLBACK_TIMEOUT")

        block = run_required_policy(_registration(callback), _runner_input())

        assert block is not None
        assert block.policy_code == RequiredPolicyFailureCode.CALLBACK_ERROR
        assert "TOP_SECRET_CALLBACK_TIMEOUT" not in json.dumps(block.to_result())

    def test_completed_future_after_deadline_remains_a_timeout(self):
        from hermes_cli import tool_policy

        future = MagicMock()

        def finish_after_deadline(*, timeout):
            assert timeout == pytest.approx(0.25)
            tool_policy._required_policy_slots.release()
            raise FuturesTimeoutError

        future.result.side_effect = finish_after_deadline
        future.done.return_value = True
        future.exception.return_value = None
        with patch.object(
            tool_policy._required_policy_executor,
            "submit",
            return_value=future,
        ):
            block = run_required_policy(_registration(_allow), _runner_input())

        assert block is not None
        assert block.policy_code == RequiredPolicyFailureCode.TIMEOUT

    def test_submit_failure_releases_slot_and_quarantines(self):
        from hermes_cli import tool_policy

        registration = _registration(_allow)
        policy_input = _runner_input()
        with patch.object(
            tool_policy._required_policy_executor,
            "submit",
            side_effect=RuntimeError("TOP_SECRET_SUBMIT_FAILURE"),
        ):
            block = run_required_policy(registration, policy_input)

        assert block is not None
        assert block.policy_code == RequiredPolicyFailureCode.CALLBACK_ERROR
        assert "TOP_SECRET_SUBMIT_FAILURE" not in json.dumps(block.to_result())
        assert is_required_policy_quarantined(
            policy_input.session_id,
            registration.plugin_key,
            registration.policy_name,
        )

    def test_timeout_quarantines_and_late_allow_has_no_authority(self):
        started = threading.Event()
        release = threading.Event()
        returned = threading.Event()
        handler_calls = []
        callback_calls = []

        def callback(payload):
            callback_calls.append(True)
            started.set()
            assert release.wait(2)
            returned.set()
            return _allow(payload)

        registration = _registration(callback, timeout_ms=20)
        policy_input = _runner_input()

        block = run_required_policy(registration, policy_input)
        if block is None:
            handler_calls.append(True)

        assert started.is_set()
        assert block is not None
        assert block.policy_code == RequiredPolicyFailureCode.TIMEOUT
        repeated = run_required_policy(registration, policy_input)
        assert repeated is not None
        assert repeated.policy_code == RequiredPolicyFailureCode.QUARANTINED
        assert callback_calls == [True]

        release.set()
        assert returned.wait(1)
        _assert_policy_slots_recovered()
        assert handler_calls == []

    @pytest.mark.parametrize(
        ("decision", "expected_code"),
        [
            ("allow", PolicyDecisionCode.MALFORMED),
            ({"action": "later"}, PolicyDecisionCode.NON_EXPLICIT),
            ({"action": "allow"}, PolicyDecisionCode.BINDING_MISSING),
            (
                {"action": "allow", "policy_binding": "0" * 64},
                PolicyDecisionCode.BINDING_MISMATCH,
            ),
            (
                {"action": "block", "message": ""},
                PolicyDecisionCode.EMPTY_BLOCK_MESSAGE,
            ),
        ],
    )
    def test_invalid_decision_quarantines(self, decision, expected_code):
        calls = []

        def callback(_payload):
            calls.append(True)
            return decision

        registration = _registration(callback)
        policy_input = _runner_input()

        first = run_required_policy(registration, policy_input)
        second = run_required_policy(registration, policy_input)

        assert first is not None
        assert first.policy_code == expected_code
        assert second is not None
        assert second.policy_code == RequiredPolicyFailureCode.QUARANTINED
        assert calls == [True]

    def test_quarantine_is_scoped_and_can_clear_one_session(self):
        calls = []

        def callback(_payload):
            calls.append(True)
            raise RuntimeError("failure")

        registration = _registration(callback)
        first_input = _runner_input("session-one")
        second_input = _runner_input("session-two")

        assert run_required_policy(registration, first_input).policy_code == (
            RequiredPolicyFailureCode.CALLBACK_ERROR
        )
        assert run_required_policy(registration, second_input).policy_code == (
            RequiredPolicyFailureCode.CALLBACK_ERROR
        )
        assert calls == [True, True]

        clear_required_policy_quarantine("session-one")
        assert not is_required_policy_quarantined(
            "session-one", "governor", "tool_dispatch"
        )
        assert is_required_policy_quarantined(
            "session-two", "governor", "tool_dispatch"
        )

    def test_real_wedged_callbacks_saturate_without_queue_growth(self):
        from hermes_cli import tool_policy

        started_count = 0
        started_lock = threading.Lock()
        all_started = threading.Event()
        release = threading.Event()
        callback_calls = []

        def callback(payload):
            nonlocal started_count
            with started_lock:
                started_count += 1
                callback_calls.append(payload["session_id"])
                if started_count == tool_policy._REQUIRED_POLICY_MAX_WORKERS:
                    all_started.set()
            assert release.wait(2)
            return _allow(payload)

        registration = _registration(callback, timeout_ms=1500)
        caller_pool = ThreadPoolExecutor(max_workers=4)
        futures = []
        try:
            futures = [
                caller_pool.submit(
                    run_required_policy,
                    registration,
                    _runner_input(f"wedged-{index}"),
                )
                for index in range(tool_policy._REQUIRED_POLICY_MAX_WORKERS)
            ]
            assert all_started.wait(1)

            saturated = run_required_policy(
                registration,
                _runner_input("saturated-extra"),
            )
            assert saturated is not None
            assert saturated.policy_code == (
                RequiredPolicyFailureCode.EXECUTOR_SATURATED
            )
            assert len(callback_calls) == 4
        finally:
            release.set()
            caller_pool.shutdown(wait=True)

        assert all(future.result() is None for future in futures)
        _assert_policy_slots_recovered()


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        ("disabled via config", RequiredPolicyFailureCode.PLUGIN_DISABLED),
        (
            "not enabled in config (run command)",
            RequiredPolicyFailureCode.PLUGIN_DISABLED,
        ),
        (
            "TOP_SECRET_PLUGIN_LOAD_FAILURE",
            RequiredPolicyFailureCode.PLUGIN_LOAD_ERROR,
        ),
    ],
)
def test_authorize_reports_safe_unavailable_plugin_state(error, expected_code):
    policy_input = _runner_input()
    manager = _manager_with_plugin(enabled=False, error=error)
    with (
        patch(
            "hermes_cli.plugins._get_required_policies_for_module",
            return_value={"governor": ["tool_dispatch"]},
        ),
        patch("hermes_cli.plugins.get_plugin_manager", return_value=manager),
    ):
        block = authorize_required_tool_policies(policy_input)

    assert block is not None
    assert block.policy == "tool_dispatch"
    assert block.policy_code == expected_code
    assert "TOP_SECRET_PLUGIN_LOAD_FAILURE" not in json.dumps(block.to_result())


def test_authorize_blocks_invalid_config_without_read_error_leak():
    with patch(
        "hermes_cli.plugins._get_required_policies_for_module",
        side_effect=RuntimeError("TOP_SECRET_CONFIG_FAILURE"),
    ):
        block = authorize_required_tool_policies(_runner_input())

    assert block is not None
    assert block.policy == "tool_dispatch"
    assert block.policy_code == RequiredPolicyFailureCode.CONFIG_INVALID
    assert "TOP_SECRET_CONFIG_FAILURE" not in json.dumps(block.to_result())


def test_authorize_empty_mapping_allows_without_discovery():
    with (
        patch(
            "hermes_cli.plugins._get_required_policies_for_module",
            return_value={},
        ),
        patch("hermes_cli.plugins.get_plugin_manager") as get_manager,
    ):
        assert authorize_required_tool_policies(_runner_input()) is None

    get_manager.assert_not_called()


def test_authorize_rejects_non_mapping_runtime_config():
    with patch(
        "hermes_cli.plugins._get_required_policies_for_module",
        return_value="invalid",
    ):
        block = authorize_required_tool_policies(_runner_input())

    assert block is not None
    assert block.policy_code == RequiredPolicyFailureCode.CONFIG_INVALID


def test_authorize_blocks_missing_plugin():
    manager = PluginManager()
    manager._discovered = True
    manager._discovery_home = plugins_mod._resolved_hermes_home()
    with (
        patch(
            "hermes_cli.plugins._get_required_policies_for_module",
            return_value={"missing": ["tool_dispatch"]},
        ),
        patch("hermes_cli.plugins.get_plugin_manager", return_value=manager),
    ):
        block = authorize_required_tool_policies(_runner_input())

    assert block is not None
    assert block.policy == "tool_dispatch"
    assert block.policy_code == RequiredPolicyFailureCode.PLUGIN_MISSING


def test_authorize_blocks_missing_registration():
    manager = _manager_with_plugin(enabled=True)
    with (
        patch(
            "hermes_cli.plugins._get_required_policies_for_module",
            return_value={"governor": ["tool_dispatch"]},
        ),
        patch("hermes_cli.plugins.get_plugin_manager", return_value=manager),
    ):
        block = authorize_required_tool_policies(_runner_input())

    assert block is not None
    assert block.policy_code == RequiredPolicyFailureCode.REGISTRATION_MISSING


def test_authorize_blocks_discovery_failure_without_exception_text():
    manager = MagicMock()
    manager.discover_and_load.side_effect = RuntimeError(
        "TOP_SECRET_DISCOVERY_FAILURE"
    )
    with (
        patch(
            "hermes_cli.plugins._get_required_policies_for_module",
            return_value={"governor": ["tool_dispatch"]},
        ),
        patch("hermes_cli.plugins.get_plugin_manager", return_value=manager),
    ):
        block = authorize_required_tool_policies(_runner_input())

    assert block is not None
    assert block.policy_code == RequiredPolicyFailureCode.PLUGIN_LOAD_ERROR
    assert "TOP_SECRET_DISCOVERY_FAILURE" not in json.dumps(block.to_result())


def test_authorize_evaluates_plugins_in_sorted_order_and_first_block_wins():
    calls = []

    def callback_for(name):
        def callback(_payload):
            calls.append(name)
            return {"action": "block", "message": name}

        return callback

    manager = PluginManager()
    manager._discovered = True
    manager._discovery_home = plugins_mod._resolved_hermes_home()
    for key in ("z-plugin", "a-plugin"):
        registration = _registration(callback_for(key), plugin_key=key)
        loaded = LoadedPlugin(
            manifest=PluginManifest(
                name=key,
                key=key,
                provides_policies=["tool_dispatch"],
            ),
            enabled=True,
        )
        manager._plugins[key] = loaded
        manager._register_policy(registration)

    with (
        patch(
            "hermes_cli.plugins._get_required_policies_for_module",
            return_value={
                "z-plugin": ["tool_dispatch"],
                "a-plugin": ["tool_dispatch"],
            },
        ),
        patch("hermes_cli.plugins.get_plugin_manager", return_value=manager),
    ):
        block = authorize_required_tool_policies(_runner_input())

    assert block is not None
    assert block.message == "a-plugin"
    assert calls == ["a-plugin"]


def test_authorize_earlier_callback_block_precedes_later_static_failure():
    calls = []

    def callback(_payload):
        calls.append("a-plugin")
        return {"action": "block", "message": "a-plugin"}

    manager = PluginManager()
    manager._discovered = True
    manager._discovery_home = plugins_mod._resolved_hermes_home()
    registration = _registration(callback, plugin_key="a-plugin")
    manager._plugins["a-plugin"] = LoadedPlugin(
        manifest=PluginManifest(
            name="a-plugin",
            key="a-plugin",
            provides_policies=["tool_dispatch"],
        ),
        enabled=True,
    )
    manager._register_policy(registration)

    with (
        patch(
            "hermes_cli.plugins._get_required_policies_for_module",
            return_value={
                "z-missing": ["tool_dispatch"],
                "a-plugin": ["tool_dispatch"],
            },
        ),
        patch("hermes_cli.plugins.get_plugin_manager", return_value=manager),
    ):
        block = authorize_required_tool_policies(_runner_input())

    assert block is not None
    assert block.policy_code == "policy_blocked"
    assert block.message == "a-plugin"
    assert calls == ["a-plugin"]


def _write_required_policy_recovery_config(
    home: Path,
    *,
    plugin_key: str = "late-governor",
    enabled: bool = True,
    disabled: bool = False,
) -> None:
    home.mkdir(parents=True, exist_ok=True)
    config = {
        "plugins": {
            "enabled": [plugin_key] if enabled else [],
            "disabled": [plugin_key] if disabled else [],
            "required_policies": {plugin_key: ["tool_dispatch"]},
        }
    }
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def _write_required_policy_recovery_plugin(
    home: Path,
    marker: Path,
    *,
    plugin_key: str = "late-governor",
) -> Path:
    plugin_dir = home / "plugins" / plugin_key
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump(
            {
                "name": plugin_key,
                "version": "1.0.0",
                "policies": ["tool_dispatch"],
            }
        ),
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "def _policy(payload):\n"
        f"    with open({str(marker)!r}, 'a', encoding='utf-8') as fh:\n"
        "        fh.write('policy\\n')\n"
        "    return {'action': 'allow', 'policy_binding': payload['policy_binding']}\n"
        "\n"
        "def _hook(**kwargs):\n"
        "    return 'late-hook'\n"
        "\n"
        "def register(ctx):\n"
        f"    with open({str(marker)!r}, 'a', encoding='utf-8') as fh:\n"
        "        fh.write('register\\n')\n"
        "    ctx.register_hook('pre_llm_call', _hook)\n"
        "    ctx.register_policy('tool_dispatch', _policy)\n",
        encoding="utf-8",
    )
    return plugin_dir


def _write_custom_required_policy_recovery_plugin(
    root: Path,
    *,
    plugin_key: str = "late-governor",
    hook_value: str = "custom-hook",
    declares_policy: bool = True,
    register_tail: str = "",
) -> Path:
    plugin_dir = root / plugin_key
    plugin_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"name": plugin_key, "version": "1.0.0"}
    if declares_policy:
        manifest["policies"] = ["tool_dispatch"]
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump(manifest),
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "def _policy(payload):\n"
        "    return {'action': 'allow', 'policy_binding': payload['policy_binding']}\n"
        "\n"
        "def _hook(**kwargs):\n"
        f"    return {hook_value!r}\n"
        "\n"
        "def register(ctx):\n"
        "    ctx.register_hook('pre_llm_call', _hook)\n"
        "    ctx.register_policy('tool_dispatch', _policy)\n"
        + (f"    {register_tail}\n" if register_tail else ""),
        encoding="utf-8",
    )
    return plugin_dir


def _write_required_policy_runtime_source(
    plugin_dir: Path,
    *,
    hook_value: str,
    policy_message: str | None = None,
) -> None:
    policy_result = (
        f"{{'action': 'block', 'message': {policy_message!r}}}"
        if policy_message is not None
        else "{'action': 'allow', 'policy_binding': payload['policy_binding']}"
    )
    (plugin_dir / "__init__.py").write_text(
        "def policy(payload):\n"
        f"    return {policy_result}\n"
        "\n"
        "def hook(**kwargs):\n"
        f"    return {hook_value!r}\n"
        "\n"
        "def register(ctx):\n"
        "    ctx.register_hook('pre_llm_call', hook)\n"
        "    ctx.register_policy('tool_dispatch', policy)\n",
        encoding="utf-8",
    )


def _empty_required_policy_recovery_manager(
    home: Path,
    tmp_path: Path,
    monkeypatch,
) -> PluginManager:
    bundled = tmp_path / "empty-bundled"
    bundled.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_SAFE_MODE", raising=False)
    monkeypatch.delenv("HERMES_ENABLE_PROJECT_PLUGINS", raising=False)
    monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: bundled)
    monkeypatch.setattr(PluginManager, "_scan_entry_points", lambda self: [])
    manager = PluginManager()
    manager.discover_and_load()
    monkeypatch.setattr(plugins_mod, "_plugin_manager", manager)
    return manager


def test_required_policy_recovery_installs_after_empty_same_pid_discovery(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    marker = tmp_path / "calls.log"
    token = set_hermes_home_override(home)
    try:
        manager = _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        manager_identity = id(manager)

        _write_required_policy_recovery_plugin(home, marker)
        _write_required_policy_recovery_config(home)

        assert plugins_mod.recover_required_policy_plugins() is True
        assert id(plugins_mod.get_plugin_manager()) == manager_identity
        assert authorize_required_tool_policies(_runner_input()) is None
        assert plugins_mod.invoke_hook("pre_llm_call") == ["late-hook"]
        assert marker.read_text(encoding="utf-8").splitlines() == [
            "register",
            "policy",
        ]
    finally:
        reset_hermes_home_override(token)


def test_late_required_policy_initially_disabled_then_enabled_registers_once(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    marker = tmp_path / "calls.log"
    token = set_hermes_home_override(home)
    try:
        _write_required_policy_recovery_plugin(home, marker)
        _write_required_policy_recovery_config(home, enabled=False)
        manager = _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        assert manager._plugins["late-governor"].enabled is False

        _write_required_policy_recovery_config(home, enabled=True)
        assert plugins_mod.recover_required_policy_plugins() is True
        assert plugins_mod.recover_required_policy_plugins() is True
        assert authorize_required_tool_policies(_runner_input()) is None

        assert marker.read_text(encoding="utf-8").splitlines().count("register") == 1
    finally:
        reset_hermes_home_override(token)


def test_required_policy_recovery_concurrent_calls_publish_once(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    marker = tmp_path / "calls.log"
    token = set_hermes_home_override(home)
    try:
        _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        _write_required_policy_recovery_plugin(home, marker)
        _write_required_policy_recovery_config(home)

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [
                executor.submit(copy_context().run, plugins_mod.recover_required_policy_plugins)
                for _ in range(8)
            ]
        assert all(future.result() is True for future in futures)
        assert marker.read_text(encoding="utf-8").splitlines() == ["register"]
        assert plugins_mod.invoke_hook("pre_llm_call") == ["late-hook"]
    finally:
        reset_hermes_home_override(token)


def test_required_policy_recovery_home_mismatch_hides_snapshot_and_callback(
    tmp_path,
    monkeypatch,
):
    home_a = tmp_path / "home-a"
    home_b = tmp_path / "home-b"
    marker = tmp_path / "calls.log"
    token_a = set_hermes_home_override(home_a)
    try:
        _empty_required_policy_recovery_manager(home_a, tmp_path, monkeypatch)
        _write_required_policy_recovery_plugin(home_a, marker)
        _write_required_policy_recovery_config(home_a)
        assert plugins_mod.recover_required_policy_plugins() is True
        assert authorize_required_tool_policies(_runner_input("home-a")) is None

        _write_required_policy_recovery_config(home_b)
        monkeypatch.setenv("HERMES_HOME", str(home_b))
        token_b = set_hermes_home_override(home_b)
        try:
            class _ExplodingPluginMap(dict):
                def get(self, *_args, **_kwargs):
                    raise AssertionError("home A plugin map was consulted under home B")

            manager = plugins_mod.get_plugin_manager()
            manager._plugins = _ExplodingPluginMap(manager._plugins)
            monkeypatch.setattr(
                manager,
                "get_policy_registration",
                MagicMock(side_effect=AssertionError("home A policy map was consulted")),
            )
            block = authorize_required_tool_policies(_runner_input("home-b"))
            assert block is not None
            assert block.policy_code == RequiredPolicyFailureCode.PLUGIN_LOAD_ERROR
            assert plugins_mod.invoke_hook("pre_llm_call") == []
        finally:
            reset_hermes_home_override(token_b)

        assert marker.read_text(encoding="utf-8").splitlines() == [
            "register",
            "policy",
        ]
    finally:
        reset_hermes_home_override(token_a)


@pytest.mark.parametrize(
    ("project_enabled", "expected_hook"),
    [(False, "user-hook"), (True, "project-hook")],
)
def test_required_policy_recovery_rescans_existing_root_and_project_precedence(
    tmp_path,
    monkeypatch,
    project_enabled,
    expected_hook,
):
    home = tmp_path / ("home-enabled" if project_enabled else "home-disabled")
    project = tmp_path / ("project-enabled" if project_enabled else "project-disabled")
    (home / "plugins").mkdir(parents=True)
    project.mkdir()
    monkeypatch.chdir(project)
    if project_enabled:
        monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "1")
    else:
        monkeypatch.delenv("HERMES_ENABLE_PROJECT_PLUGINS", raising=False)
    token = set_hermes_home_override(home)
    try:
        _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        if project_enabled:
            monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "1")
        _write_custom_required_policy_recovery_plugin(
            home / "plugins",
            hook_value="user-hook",
        )
        _write_custom_required_policy_recovery_plugin(
            project / ".hermes" / "plugins",
            hook_value="project-hook",
        )
        _write_required_policy_recovery_config(home)

        assert plugins_mod.recover_required_policy_plugins() is True
        assert authorize_required_tool_policies(_runner_input()) is None
        assert plugins_mod.invoke_hook("pre_llm_call") == [expected_hook]
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize(
    "failure_mode",
    ["safe_mode", "disabled", "undeclared", "context_drift", "load_failure", "entrypoint"],
)
def test_required_policy_recovery_fail_closed_boundaries(
    tmp_path,
    monkeypatch,
    failure_mode,
):
    home = tmp_path / f"home-{failure_mode}"
    token = set_hermes_home_override(home)
    try:
        manager = _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        register_tail = ""
        declares_policy = failure_mode != "undeclared"
        if failure_mode == "context_drift":
            register_tail = (
                "__import__('hermes_constants').set_hermes_home_override("
                f"{str(tmp_path / 'drifted-home')!r})"
            )
        elif failure_mode == "load_failure":
            register_tail = "raise RuntimeError('staging failed')"

        if failure_mode == "entrypoint":
            manager._plugins["late-governor"] = LoadedPlugin(
                manifest=PluginManifest(
                    name="late-governor",
                    key="late-governor",
                    source="entrypoint",
                    path=None,
                    provides_policies=["tool_dispatch"],
                ),
                enabled=False,
                error="not enabled in config",
            )
        else:
            _write_custom_required_policy_recovery_plugin(
                home / "plugins",
                declares_policy=declares_policy,
                register_tail=register_tail,
            )

        _write_required_policy_recovery_config(
            home,
            disabled=failure_mode == "disabled",
        )
        if failure_mode == "safe_mode":
            monkeypatch.setenv("HERMES_SAFE_MODE", "1")

        assert plugins_mod.recover_required_policy_plugins() is False
        block = authorize_required_tool_policies(_runner_input(failure_mode))
        assert block is not None
        assert plugins_mod.invoke_hook("pre_llm_call") == []
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize(
    "forbidden_surface",
    ["register_tool", "register_provider", "register_platform", "register_middleware"],
)
def test_required_policy_recovery_forbidden_registration_publishes_nothing(
    tmp_path,
    monkeypatch,
    forbidden_surface,
):
    from tools.registry import registry

    home = tmp_path / f"home-{forbidden_surface}"
    token = set_hermes_home_override(home)
    try:
        _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        _write_custom_required_policy_recovery_plugin(
            home / "plugins",
            register_tail=f"getattr(ctx, {forbidden_surface!r})",
        )
        _write_required_policy_recovery_config(home)
        generation_before = registry._generation

        assert plugins_mod.recover_required_policy_plugins() is False
        assert registry._generation == generation_before
        assert plugins_mod.invoke_hook("pre_llm_call") == []
        assert authorize_required_tool_policies(_runner_input()) is not None
    finally:
        reset_hermes_home_override(token)


def test_required_policy_recovery_reader_observes_complete_old_snapshot(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    token = set_hermes_home_override(home)
    try:
        _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        _write_custom_required_policy_recovery_plugin(home / "plugins")
        _write_required_policy_recovery_config(home)

        real_capture = plugins_mod._capture_required_policy_runtime_snapshot
        reader_captured = threading.Event()
        release_reader = threading.Event()

        def paused_capture():
            snapshot = real_capture()
            if threading.current_thread().name.startswith("paused-snapshot-reader"):
                reader_captured.set()
                assert release_reader.wait(2)
            return snapshot

        monkeypatch.setattr(
            plugins_mod,
            "_capture_required_policy_runtime_snapshot",
            paused_capture,
        )
        with ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="paused-snapshot-reader",
        ) as executor:
            old_reader = executor.submit(
                copy_context().run,
                plugins_mod.invoke_hook,
                "pre_llm_call",
            )
            assert reader_captured.wait(1)
            recovery = executor.submit(
                copy_context().run,
                plugins_mod.recover_required_policy_plugins,
            )
            assert recovery.done() is False
            release_reader.set()
            assert old_reader.result(timeout=1) == []
            assert recovery.result(timeout=1) is True

        new_snapshot = real_capture()
        active_home = str(home.resolve())
        assert len([item for item in new_snapshot.plugins if item.home == active_home]) == 1
        assert len([item for item in new_snapshot.hooks if item.home == active_home]) == 1
        assert len([item for item in new_snapshot.policies if item.home == active_home]) == 1
        assert plugins_mod.invoke_hook("pre_llm_call") == ["custom-hook"]
    finally:
        reset_hermes_home_override(token)


def test_required_policy_recovery_discards_all_private_modules_on_batch_failure(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    token = set_hermes_home_override(home)
    try:
        _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        _write_custom_required_policy_recovery_plugin(
            home / "plugins",
            plugin_key="a-good",
            hook_value="must-not-publish",
        )
        _write_custom_required_policy_recovery_plugin(
            home / "plugins",
            plugin_key="z-bad",
            register_tail="raise RuntimeError('later candidate failed')",
        )
        (home / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "plugins": {
                        "enabled": ["a-good", "z-bad"],
                        "required_policies": {
                            "a-good": ["tool_dispatch"],
                            "z-bad": ["tool_dispatch"],
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        private_before = {
            name
            for name in sys.modules
            if name.startswith("_hermes_required_policy_recovery_")
        }

        assert plugins_mod.recover_required_policy_plugins() is False
        private_after = {
            name
            for name in sys.modules
            if name.startswith("_hermes_required_policy_recovery_")
        }
        assert private_after == private_before
        assert plugins_mod.invoke_hook("pre_llm_call") == []
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize("source", ["bundled", "entrypoint"])
def test_required_policy_recovery_refuses_existing_manifest_outside_active_roots(
    tmp_path,
    monkeypatch,
    source,
):
    home = tmp_path / f"home-{source}"
    external_root = tmp_path / f"external-{source}"
    token = set_hermes_home_override(home)
    try:
        manager = _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        plugin_dir = _write_custom_required_policy_recovery_plugin(external_root)
        manager._plugins["late-governor"] = LoadedPlugin(
            manifest=PluginManifest(
                name="late-governor",
                key="late-governor",
                source=source,
                path=str(plugin_dir) if source != "entrypoint" else None,
                provides_policies=["tool_dispatch"],
            ),
            enabled=False,
            error="not enabled in config",
        )
        _write_required_policy_recovery_config(home)

        assert plugins_mod.recover_required_policy_plugins() is False
        assert plugins_mod.invoke_hook("pre_llm_call") == []
    finally:
        reset_hermes_home_override(token)


def test_required_policy_recovery_refuses_user_symlink_outside_active_home(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    external_root = tmp_path / "external"
    token = set_hermes_home_override(home)
    try:
        _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        plugin_dir = _write_custom_required_policy_recovery_plugin(external_root)
        (home / "plugins").mkdir(parents=True, exist_ok=True)
        (home / "plugins" / "late-governor").symlink_to(
            plugin_dir,
            target_is_directory=True,
        )
        _write_required_policy_recovery_config(home)

        assert plugins_mod.recover_required_policy_plugins() is False
        assert plugins_mod.invoke_hook("pre_llm_call") == []
    finally:
        reset_hermes_home_override(token)


def test_required_policy_recovery_preserves_legacy_manifest_name_enablement(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    token = set_hermes_home_override(home)
    try:
        _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        plugin_dir = home / "plugins" / "category" / "governor"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "governor",
                    "version": "1.0.0",
                    "policies": ["tool_dispatch"],
                }
            ),
            encoding="utf-8",
        )
        (plugin_dir / "__init__.py").write_text(
            "def policy(payload):\n"
            "    return {'action': 'allow', 'policy_binding': payload['policy_binding']}\n"
            "\n"
            "def register(ctx):\n"
            "    ctx.register_policy('tool_dispatch', policy)\n",
            encoding="utf-8",
        )
        (home / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "plugins": {
                        "enabled": ["governor"],
                        "required_policies": {
                            "category/governor": ["tool_dispatch"]
                        },
                    }
                }
            ),
            encoding="utf-8",
        )

        assert plugins_mod.recover_required_policy_plugins() is True
        block = authorize_required_tool_policies(_runner_input())
        assert block is None
    finally:
        reset_hermes_home_override(token)


def test_required_policy_recovery_rejects_requirement_drift_during_staging(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    token = set_hermes_home_override(home)
    try:
        _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        replacement_config = yaml.safe_dump(
            {
                "plugins": {
                    "enabled": ["late-governor"],
                    "required_policies": {
                        "replacement-governor": ["tool_dispatch"]
                    },
                }
            }
        )
        _write_custom_required_policy_recovery_plugin(
            home / "plugins",
            register_tail=(
                f"open({str(home / 'config.yaml')!r}, 'w', encoding='utf-8')"
                f".write({replacement_config!r})"
            ),
        )
        _write_required_policy_recovery_config(home)

        assert plugins_mod.recover_required_policy_plugins() is False
        assert plugins_mod.invoke_hook("pre_llm_call") == []
        assert plugins_mod._get_required_policies_for_module() == {
            "replacement-governor": ["tool_dispatch"]
        }
    finally:
        reset_hermes_home_override(token)


def test_authorize_required_policy_recovery_uses_post_recovery_requirements(
    monkeypatch,
):
    callback_calls = []
    manager = _manager_with_plugin(
        plugin_key="old-governor",
        enabled=True,
        registration=_registration(
            lambda payload: callback_calls.append(payload) or _allow(payload),
            plugin_key="old-governor",
        ),
    )
    policy_state = {
        "required": {"old-governor": ["tool_dispatch"]},
    }

    def recover_and_change_requirements():
        policy_state["required"] = {
            "replacement-governor": ["tool_dispatch"]
        }
        return False

    monkeypatch.setattr(
        plugins_mod,
        "_get_required_policies_for_module",
        lambda: policy_state["required"],
    )
    monkeypatch.setattr(plugins_mod, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(
        plugins_mod,
        "recover_required_policy_plugins",
        recover_and_change_requirements,
    )

    block = authorize_required_tool_policies(_runner_input())

    assert block is not None
    assert block.policy_code == RequiredPolicyFailureCode.PLUGIN_MISSING
    assert callback_calls == []


def test_required_policy_recovery_serializes_first_discovery(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    marker = tmp_path / "calls.log"
    token = set_hermes_home_override(home)
    try:
        bundled = tmp_path / "empty-bundled"
        bundled.mkdir()
        monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: bundled)
        monkeypatch.setattr(PluginManager, "_scan_entry_points", lambda self: [])
        _write_required_policy_recovery_plugin(home, marker)
        _write_required_policy_recovery_config(home)
        manager = PluginManager()
        monkeypatch.setattr(plugins_mod, "_plugin_manager", manager)

        real_discover = manager.discover_and_load
        state_lock = threading.Lock()
        first_entered = threading.Event()
        second_started = threading.Event()
        second_entered = threading.Event()
        release_first = threading.Event()
        active_calls = 0
        max_active_calls = 0
        call_count = 0

        def instrumented_discover(force=False):
            nonlocal active_calls, max_active_calls, call_count
            with state_lock:
                call_count += 1
                this_call = call_count
                active_calls += 1
                max_active_calls = max(max_active_calls, active_calls)
                if this_call == 1:
                    first_entered.set()
                else:
                    second_entered.set()
            try:
                if this_call == 1:
                    assert release_first.wait(2)
                return real_discover(force=force)
            finally:
                with state_lock:
                    active_calls -= 1

        def second_recovery():
            second_started.set()
            return plugins_mod.recover_required_policy_plugins()

        monkeypatch.setattr(manager, "discover_and_load", instrumented_discover)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                copy_context().run,
                plugins_mod.recover_required_policy_plugins,
            )
            assert first_entered.wait(1)
            second = executor.submit(copy_context().run, second_recovery)
            assert second_started.wait(1)
            second_entered.wait(0.5)
            release_first.set()
            assert first.result(timeout=2) is True
            assert second.result(timeout=2) is True

        assert max_active_calls == 1
        assert marker.read_text(encoding="utf-8").splitlines() == ["register"]
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize("raises_after_drift", [False, True])
def test_required_policy_recovery_restores_home_after_module_import_drift(
    tmp_path,
    monkeypatch,
    raises_after_drift,
):
    home = tmp_path / "home"
    drifted_home = tmp_path / "drifted-home"
    token = set_hermes_home_override(home)
    try:
        _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        plugin_dir = home / "plugins" / "late-governor"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "late-governor",
                    "version": "1.0.0",
                    "policies": ["tool_dispatch"],
                }
            ),
            encoding="utf-8",
        )
        import_tail = "raise RuntimeError('import failed after drift')\n" if raises_after_drift else ""
        (plugin_dir / "__init__.py").write_text(
            "from hermes_constants import set_hermes_home_override\n"
            f"set_hermes_home_override({str(drifted_home)!r})\n"
            + import_tail
            + "def policy(payload):\n"
            "    return {'action': 'allow', 'policy_binding': payload['policy_binding']}\n"
            "\n"
            "def register(ctx):\n"
            "    ctx.register_policy('tool_dispatch', policy)\n",
            encoding="utf-8",
        )
        _write_required_policy_recovery_config(home)

        assert plugins_mod.recover_required_policy_plugins() is False
        assert plugins_mod._resolved_hermes_home() == str(home.resolve())
        assert plugins_mod.invoke_hook("pre_llm_call") == []
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize("linked_file", ["plugin.yaml", "__init__.py"])
def test_required_policy_recovery_refuses_inner_file_symlink_escape(
    tmp_path,
    monkeypatch,
    linked_file,
):
    home = tmp_path / "home"
    token = set_hermes_home_override(home)
    try:
        _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        plugin_dir = home / "plugins" / "late-governor"
        plugin_dir.mkdir(parents=True)
        manifest_text = yaml.safe_dump(
            {
                "name": "late-governor",
                "version": "1.0.0",
                "policies": ["tool_dispatch"],
            }
        )
        module_text = (
            "def policy(payload):\n"
            "    return {'action': 'allow', 'policy_binding': payload['policy_binding']}\n"
            "\n"
            "def register(ctx):\n"
            "    ctx.register_policy('tool_dispatch', policy)\n"
        )
        contents = {"plugin.yaml": manifest_text, "__init__.py": module_text}
        external_file = tmp_path / f"external-{linked_file.replace('.', '-')}"
        external_file.write_text(contents[linked_file], encoding="utf-8")
        for filename, content in contents.items():
            target = plugin_dir / filename
            if filename == linked_file:
                target.symlink_to(external_file)
            else:
                target.write_text(content, encoding="utf-8")
        _write_required_policy_recovery_config(home)

        assert plugins_mod.recover_required_policy_plugins() is False
        assert plugins_mod.invoke_hook("pre_llm_call") == []
    finally:
        reset_hermes_home_override(token)


def test_required_policy_recovery_does_not_retry_prior_ordinary_load_failure(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    marker = tmp_path / "calls.log"
    token = set_hermes_home_override(home)
    try:
        manager = _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        plugin_dir = _write_required_policy_recovery_plugin(home, marker)
        manager._plugins["late-governor"] = LoadedPlugin(
            manifest=PluginManifest(
                name="late-governor",
                key="late-governor",
                source="user",
                path=str(plugin_dir),
                provides_policies=["tool_dispatch"],
            ),
            enabled=False,
            error="ordinary import exploded",
        )
        _write_required_policy_recovery_config(home)

        assert plugins_mod.recover_required_policy_plugins() is False
        assert not marker.exists()
        assert plugins_mod.invoke_hook("pre_llm_call") == []
    finally:
        reset_hermes_home_override(token)


def test_required_policy_recovery_seals_retained_context_after_register(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    token = set_hermes_home_override(home)
    try:
        _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        plugin_dir = home / "plugins" / "late-governor"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "late-governor",
                    "version": "1.0.0",
                    "policies": ["tool_dispatch"],
                }
            ),
            encoding="utf-8",
        )
        (plugin_dir / "__init__.py").write_text(
            "retained_context = None\n"
            "\n"
            "def policy(payload):\n"
            "    return {'action': 'allow', 'policy_binding': payload['policy_binding']}\n"
            "\n"
            "def register(ctx):\n"
            "    global retained_context\n"
            "    retained_context = ctx\n"
            "    ctx.register_policy('tool_dispatch', policy)\n",
            encoding="utf-8",
        )
        _write_required_policy_recovery_config(home)

        assert plugins_mod.recover_required_policy_plugins() is True
        snapshot = plugins_mod._capture_required_policy_runtime_snapshot()
        module = next(
            item for item in snapshot.modules if hasattr(item, "retained_context")
        )
        with pytest.raises(PermissionError):
            module.retained_context.register_hook("pre_llm_call", lambda: None)
        with pytest.raises(PermissionError):
            module.retained_context.register_policy(
                "tool_dispatch",
                lambda payload: _allow(payload),
            )
    finally:
        reset_hermes_home_override(token)


def test_required_policy_recovery_skips_rescan_when_snapshot_is_satisfied(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    token = set_hermes_home_override(home)
    try:
        _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        _write_custom_required_policy_recovery_plugin(home / "plugins")
        _write_required_policy_recovery_config(home)
        assert plugins_mod.recover_required_policy_plugins() is True

        scan = MagicMock(
            side_effect=AssertionError("satisfied recovery must not rescan directories")
        )
        monkeypatch.setattr(
            plugins_mod,
            "_scan_required_policy_recovery_candidates",
            scan,
        )

        assert plugins_mod.recover_required_policy_plugins() is True
        scan.assert_not_called()
    finally:
        reset_hermes_home_override(token)


def test_authorize_required_policy_recovery_serializes_first_discovery(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    marker = tmp_path / "calls.log"
    token = set_hermes_home_override(home)
    try:
        bundled = tmp_path / "empty-bundled"
        bundled.mkdir()
        monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: bundled)
        monkeypatch.setattr(PluginManager, "_scan_entry_points", lambda self: [])
        _write_required_policy_recovery_plugin(home, marker)
        _write_required_policy_recovery_config(home)
        manager = PluginManager()
        monkeypatch.setattr(plugins_mod, "_plugin_manager", manager)

        real_discover = manager.discover_and_load
        state_lock = threading.Lock()
        first_entered = threading.Event()
        second_started = threading.Event()
        second_entered = threading.Event()
        release_first = threading.Event()
        active_calls = 0
        max_active_calls = 0
        call_count = 0

        def instrumented_discover(force=False):
            nonlocal active_calls, max_active_calls, call_count
            with state_lock:
                call_count += 1
                this_call = call_count
                active_calls += 1
                max_active_calls = max(max_active_calls, active_calls)
                if this_call == 1:
                    first_entered.set()
                else:
                    second_entered.set()
            try:
                if this_call == 1:
                    assert release_first.wait(2)
                return real_discover(force=force)
            finally:
                with state_lock:
                    active_calls -= 1

        def second_authorization():
            second_started.set()
            return authorize_required_tool_policies(_runner_input("second"))

        monkeypatch.setattr(manager, "discover_and_load", instrumented_discover)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                copy_context().run,
                authorize_required_tool_policies,
                _runner_input("first"),
            )
            assert first_entered.wait(1)
            second = executor.submit(copy_context().run, second_authorization)
            assert second_started.wait(1)
            second_entered.wait(0.5)
            release_first.set()
            assert first.result(timeout=2) is None
            assert second.result(timeout=2) is None

        assert max_active_calls == 1
        assert marker.read_text(encoding="utf-8").splitlines() == [
            "register",
            "policy",
            "policy",
        ]
    finally:
        reset_hermes_home_override(token)


def test_required_policy_recovery_seal_and_snapshot_is_atomic():
    manifest = PluginManifest(
        name="late-governor",
        key="late-governor",
        provides_policies=["tool_dispatch"],
    )
    context = plugins_mod._RequiredPolicyRecoveryContext(manifest)
    append_entered = threading.Event()
    release_append = threading.Event()
    seal_finished = threading.Event()

    class BlockingHookList(list):
        def append(self, item):
            append_entered.set()
            assert release_append.wait(2)
            super().append(item)

    object.__setattr__(context, "_hooks", BlockingHookList())
    callback = lambda: None

    def seal_and_snapshot():
        try:
            seal = object.__getattribute__(context, "_seal_and_snapshot")
            return seal()
        finally:
            seal_finished.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        registration = executor.submit(
            context.register_hook,
            "pre_llm_call",
            callback,
        )
        assert append_entered.wait(1)
        sealing = executor.submit(seal_and_snapshot)
        sealed_before_append = seal_finished.wait(0.5)
        release_append.set()
        registration.result(timeout=1)
        hooks, policies = sealing.result(timeout=1)

    assert sealed_before_append is False
    assert hooks == (("pre_llm_call", callback),)
    assert policies == ()
    with pytest.raises(PermissionError):
        context.register_hook("pre_llm_call", callback)


@pytest.mark.parametrize("linked_file", ["plugin.yaml", "__init__.py"])
def test_required_policy_recovery_refuses_cross_plugin_inner_file_symlink(
    tmp_path,
    monkeypatch,
    linked_file,
):
    home = tmp_path / "home"
    token = set_hermes_home_override(home)
    try:
        _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        plugins_root = home / "plugins"
        selected_dir = plugins_root / "selected-governor"
        other_dir = plugins_root / "other-governor"
        selected_dir.mkdir(parents=True)
        other_dir.mkdir()

        def manifest_text(name):
            return yaml.safe_dump(
                {
                    "name": name,
                    "version": "1.0.0",
                    "policies": ["tool_dispatch"],
                }
            )

        module_text = (
            "def policy(payload):\n"
            "    return {'action': 'allow', 'policy_binding': payload['policy_binding']}\n"
            "\n"
            "def register(ctx):\n"
            "    ctx.register_policy('tool_dispatch', policy)\n"
        )
        selected_contents = {
            "plugin.yaml": manifest_text("selected-governor"),
            "__init__.py": module_text,
        }
        other_contents = {
            "plugin.yaml": manifest_text("other-governor"),
            "__init__.py": module_text,
        }
        for filename, content in other_contents.items():
            (other_dir / filename).write_text(content, encoding="utf-8")
        for filename, content in selected_contents.items():
            target = selected_dir / filename
            if filename == linked_file:
                target.symlink_to(other_dir / filename)
            else:
                target.write_text(content, encoding="utf-8")
        _write_required_policy_recovery_config(
            home,
            plugin_key="selected-governor",
        )

        assert plugins_mod.recover_required_policy_plugins() is False
        assert plugins_mod.invoke_hook("pre_llm_call") == []
    finally:
        reset_hermes_home_override(token)


def test_authorize_required_policy_recovery_safe_mode_skips_loaded_callback(
    monkeypatch,
):
    callback_calls = []
    manager = _manager_with_plugin(
        enabled=True,
        registration=_registration(
            lambda payload: callback_calls.append(payload) or _allow(payload),
        ),
    )
    monkeypatch.setenv("HERMES_SAFE_MODE", "1")
    monkeypatch.setattr(
        plugins_mod,
        "_get_required_policies_for_module",
        lambda: {"governor": ["tool_dispatch"]},
    )
    monkeypatch.setattr(plugins_mod, "get_plugin_manager", lambda: manager)

    block = authorize_required_tool_policies(_runner_input("safe-mode"))

    assert block is not None
    assert block.policy_code == RequiredPolicyFailureCode.PLUGIN_LOAD_ERROR
    assert callback_calls == []


@pytest.mark.parametrize("recorded_source", ["bundled", "entrypoint"])
def test_required_policy_recovery_refuses_directory_collision_with_recorded_winner(
    tmp_path,
    monkeypatch,
    recorded_source,
):
    home = tmp_path / f"home-{recorded_source}"
    marker = tmp_path / f"calls-{recorded_source}.log"
    token = set_hermes_home_override(home)
    try:
        manager = _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        _write_required_policy_recovery_plugin(home, marker)
        manager._plugins["late-governor"] = LoadedPlugin(
            manifest=PluginManifest(
                name="late-governor",
                key="late-governor",
                source=recorded_source,
                path=(
                    str(tmp_path / "bundled-late-governor")
                    if recorded_source == "bundled"
                    else None
                ),
                provides_policies=["tool_dispatch"],
            ),
            enabled=False,
            error="not enabled in config",
        )
        _write_required_policy_recovery_config(home)

        assert plugins_mod.recover_required_policy_plugins() is False
        assert not marker.exists()
        assert plugins_mod.invoke_hook("pre_llm_call") == []
    finally:
        reset_hermes_home_override(token)


def test_required_policy_recovery_safe_mode_hides_all_module_hooks(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    token = set_hermes_home_override(home)
    try:
        manager = _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        _write_custom_required_policy_recovery_plugin(
            home / "plugins",
            hook_value="recovered-hook",
        )
        _write_required_policy_recovery_config(home)
        assert plugins_mod.recover_required_policy_plugins() is True
        manager._hooks["pre_llm_call"] = [lambda **_kwargs: "ordinary-hook"]
        assert plugins_mod.invoke_hook("pre_llm_call") == [
            "ordinary-hook",
            "recovered-hook",
        ]
        assert plugins_mod.has_hook("pre_llm_call") is True

        monkeypatch.setenv("HERMES_SAFE_MODE", "1")

        assert plugins_mod.invoke_hook("pre_llm_call") == []
        assert plugins_mod.has_hook("pre_llm_call") is False
    finally:
        reset_hermes_home_override(token)


def test_force_rediscover_retires_same_home_recovered_runtime(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    token = set_hermes_home_override(home)
    try:
        manager = _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        plugin_dir = _write_custom_required_policy_recovery_plugin(
            home / "plugins",
            hook_value="recovered-old-hook",
        )
        _write_required_policy_recovery_config(home)
        assert plugins_mod.recover_required_policy_plugins() is True
        assert plugins_mod.invoke_hook("pre_llm_call") == ["recovered-old-hook"]

        (plugin_dir / "__init__.py").write_text(
            "def policy(payload):\n"
            "    return {'action': 'block', 'message': 'ordinary-current-policy'}\n"
            "\n"
            "def hook(**kwargs):\n"
            "    return 'ordinary-current-hook'\n"
            "\n"
            "def register(ctx):\n"
            "    ctx.register_hook('pre_llm_call', hook)\n"
            "    ctx.register_policy('tool_dispatch', policy)\n",
            encoding="utf-8",
        )

        manager.discover_and_load(force=True)

        assert plugins_mod.invoke_hook("pre_llm_call") == [
            "ordinary-current-hook"
        ]
        block = authorize_required_tool_policies(_runner_input("after-force"))
        assert block is not None
        assert block.message == "ordinary-current-policy"
        snapshot = plugins_mod._capture_required_policy_runtime_snapshot()
        assert not any(item.home == str(home.resolve()) for item in snapshot.plugins)
    finally:
        reset_hermes_home_override(token)


def test_required_policy_recovery_serializes_against_force_rediscovery(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    token = set_hermes_home_override(home)
    try:
        manager = _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        _write_custom_required_policy_recovery_plugin(home / "plugins")
        _write_required_policy_recovery_config(home)

        real_stage = plugins_mod._stage_required_policy_recovery_plugin
        real_inner = manager._discover_and_load_inner
        stage_entered = threading.Event()
        force_started = threading.Event()
        force_inner_entered = threading.Event()
        release_stage = threading.Event()

        def paused_stage(*args, **kwargs):
            stage_entered.set()
            assert release_stage.wait(2)
            return real_stage(*args, **kwargs)

        def observed_inner():
            force_inner_entered.set()
            return real_inner()

        def force_rediscover():
            force_started.set()
            manager.discover_and_load(force=True)

        monkeypatch.setattr(
            plugins_mod,
            "_stage_required_policy_recovery_plugin",
            paused_stage,
        )
        monkeypatch.setattr(manager, "_discover_and_load_inner", observed_inner)
        with ThreadPoolExecutor(max_workers=2) as executor:
            recovery = executor.submit(
                copy_context().run,
                plugins_mod.recover_required_policy_plugins,
            )
            assert stage_entered.wait(1)
            forced = executor.submit(copy_context().run, force_rediscover)
            assert force_started.wait(1)
            force_entered_before_release = force_inner_entered.wait(0.5)
            release_stage.set()
            assert recovery.result(timeout=2) is True
            forced.result(timeout=2)

        assert force_entered_before_release is False
        assert plugins_mod.invoke_hook("pre_llm_call") == ["custom-hook"]
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize("candidate_source", ["user", "project"])
def test_required_policy_recovery_refuses_plugins_root_symlink_escape(
    tmp_path,
    monkeypatch,
    candidate_source,
):
    home = tmp_path / f"home-{candidate_source}"
    project = tmp_path / f"project-{candidate_source}"
    external_plugins = tmp_path / f"external-plugins-{candidate_source}"
    project.mkdir()
    monkeypatch.chdir(project)
    token = set_hermes_home_override(home)
    try:
        _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        if candidate_source == "user":
            home.mkdir(parents=True, exist_ok=True)
            plugins_parent = home
        else:
            monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "1")
            (project / ".hermes").mkdir()
            plugins_parent = project / ".hermes"
        _write_custom_required_policy_recovery_plugin(external_plugins)
        (plugins_parent / "plugins").symlink_to(
            external_plugins,
            target_is_directory=True,
        )
        _write_required_policy_recovery_config(home)

        assert plugins_mod.recover_required_policy_plugins() is False
        assert plugins_mod.invoke_hook("pre_llm_call") == []
    finally:
        reset_hermes_home_override(token)


def test_required_policy_recovery_accepts_plugin_yml_fallback(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    token = set_hermes_home_override(home)
    try:
        _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        plugin_dir = home / "plugins" / "late-governor"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.yml").write_text(
            yaml.safe_dump(
                {
                    "name": "late-governor",
                    "version": "1.0.0",
                    "policies": ["tool_dispatch"],
                }
            ),
            encoding="utf-8",
        )
        (plugin_dir / "__init__.py").write_text(
            "def policy(payload):\n"
            "    return {'action': 'allow', 'policy_binding': payload['policy_binding']}\n"
            "\n"
            "def hook(**kwargs):\n"
            "    return 'yml-hook'\n"
            "\n"
            "def register(ctx):\n"
            "    ctx.register_hook('pre_llm_call', hook)\n"
            "    ctx.register_policy('tool_dispatch', policy)\n",
            encoding="utf-8",
        )
        _write_required_policy_recovery_config(home)

        assert plugins_mod.recover_required_policy_plugins() is True
        assert plugins_mod.invoke_hook("pre_llm_call") == ["yml-hook"]
        assert authorize_required_tool_policies(_runner_input("yml")) is None
    finally:
        reset_hermes_home_override(token)


def test_invoke_hook_force_capture_never_drops_generation(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    token = set_hermes_home_override(home)
    try:
        manager = _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        plugin_dir = _write_custom_required_policy_recovery_plugin(
            home / "plugins",
            hook_value="old-hook",
        )
        _write_required_policy_recovery_config(home)
        assert plugins_mod.recover_required_policy_plugins() is True
        _write_required_policy_runtime_source(
            plugin_dir,
            hook_value="new-hook",
        )

        real_capture = plugins_mod._capture_required_policy_runtime_snapshot
        capture_entered = threading.Event()
        release_capture = threading.Event()
        force_done = threading.Event()

        def paused_capture():
            capture_entered.set()
            assert release_capture.wait(2)
            return real_capture()

        def force_rediscover():
            try:
                manager.discover_and_load(force=True)
            finally:
                force_done.set()

        monkeypatch.setattr(
            plugins_mod,
            "_capture_required_policy_runtime_snapshot",
            paused_capture,
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            invocation = executor.submit(
                copy_context().run,
                plugins_mod.invoke_hook,
                "pre_llm_call",
            )
            assert capture_entered.wait(1)
            forced = executor.submit(copy_context().run, force_rediscover)
            force_finished_before_capture = force_done.wait(0.5)
            release_capture.set()
            result = invocation.result(timeout=2)
            forced.result(timeout=2)

        assert force_finished_before_capture is False
        assert len(result) == 1
        assert result[0] in {"old-hook", "new-hook"}
    finally:
        reset_hermes_home_override(token)


def test_invoke_hook_force_capture_never_doubles_generation(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    token = set_hermes_home_override(home)
    try:
        manager = _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        plugin_dir = _write_custom_required_policy_recovery_plugin(
            home / "plugins",
            hook_value="old-hook",
        )
        _write_required_policy_recovery_config(home)
        assert plugins_mod.recover_required_policy_plugins() is True
        _write_required_policy_runtime_source(
            plugin_dir,
            hook_value="new-hook",
        )

        real_retire = plugins_mod._retire_required_policy_recovery_home
        retire_entered = threading.Event()
        release_retire = threading.Event()
        invocation_done = threading.Event()

        def paused_retire(home_value):
            retire_entered.set()
            assert release_retire.wait(2)
            return real_retire(home_value)

        def invoke_and_mark():
            try:
                return plugins_mod.invoke_hook("pre_llm_call")
            finally:
                invocation_done.set()

        monkeypatch.setattr(
            plugins_mod,
            "_retire_required_policy_recovery_home",
            paused_retire,
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            forced = executor.submit(
                copy_context().run,
                manager.discover_and_load,
                True,
            )
            assert retire_entered.wait(1)
            invocation = executor.submit(copy_context().run, invoke_and_mark)
            invocation_finished_before_retire = invocation_done.wait(0.5)
            release_retire.set()
            forced.result(timeout=2)
            result = invocation.result(timeout=2)

        assert invocation_finished_before_retire is False
        assert result == ["new-hook"]
    finally:
        reset_hermes_home_override(token)


def test_invoke_hook_old_generation_keeps_lazy_relative_imports(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    token = set_hermes_home_override(home)
    try:
        manager = _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        plugin_dir = home / "plugins" / "late-governor"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "late-governor",
                    "version": "1.0.0",
                    "policies": ["tool_dispatch"],
                }
            ),
            encoding="utf-8",
        )
        (plugin_dir / "helper.py").write_text(
            "VALUE = 'lazy-old-hook'\n",
            encoding="utf-8",
        )
        (plugin_dir / "__init__.py").write_text(
            "def policy(payload):\n"
            "    return {'action': 'allow', 'policy_binding': payload['policy_binding']}\n"
            "\n"
            "def hook(**kwargs):\n"
            "    pause_entered.set()\n"
            "    assert release_pause.wait(2)\n"
            "    from .helper import VALUE\n"
            "    return VALUE\n"
            "\n"
            "def register(ctx):\n"
            "    ctx.register_hook('pre_llm_call', hook)\n"
            "    ctx.register_policy('tool_dispatch', policy)\n",
            encoding="utf-8",
        )
        _write_required_policy_recovery_config(home)
        assert plugins_mod.recover_required_policy_plugins() is True
        snapshot = plugins_mod._capture_required_policy_runtime_snapshot()
        private_module = next(
            module
            for module in snapshot.modules
            if module.__name__.startswith("_hermes_required_policy_recovery_")
        )
        pause_entered = threading.Event()
        release_pause = threading.Event()
        force_done = threading.Event()
        private_module.pause_entered = pause_entered
        private_module.release_pause = release_pause

        def force_rediscover():
            try:
                manager.discover_and_load(force=True)
            finally:
                force_done.set()

        with ThreadPoolExecutor(max_workers=2) as executor:
            invocation = executor.submit(
                copy_context().run,
                plugins_mod.invoke_hook,
                "pre_llm_call",
            )
            assert pause_entered.wait(1)
            forced = executor.submit(copy_context().run, force_rediscover)
            force_completed_outside_callback = force_done.wait(1)
            root_retained = private_module.__name__ in sys.modules
            release_pause.set()
            forced.result(timeout=2)
            result = invocation.result(timeout=2)

        assert force_completed_outside_callback is True
        assert root_retained is True
        assert result == ["lazy-old-hook"]
    finally:
        reset_hermes_home_override(token)


def test_authorize_capture_cannot_substitute_other_home_generation(
    tmp_path,
    monkeypatch,
):
    home_a = tmp_path / "home-a"
    home_b = tmp_path / "home-b"
    bundled = tmp_path / "empty-bundled"
    bundled.mkdir()
    monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: bundled)
    monkeypatch.setattr(PluginManager, "_scan_entry_points", lambda self: [])
    plugin_a = _write_custom_required_policy_recovery_plugin(home_a / "plugins")
    plugin_b = _write_custom_required_policy_recovery_plugin(home_b / "plugins")
    _write_required_policy_runtime_source(
        plugin_a,
        hook_value="home-a-hook",
        policy_message="home-a-policy",
    )
    _write_required_policy_runtime_source(
        plugin_b,
        hook_value="home-b-hook",
        policy_message="home-b-policy",
    )
    _write_required_policy_recovery_config(home_a)
    _write_required_policy_recovery_config(home_b)
    token_a = set_hermes_home_override(home_a)
    try:
        manager = PluginManager()
        manager.discover_and_load()
        monkeypatch.setattr(plugins_mod, "_plugin_manager", manager)

        real_capture = plugins_mod._capture_required_policy_runtime_snapshot
        capture_count = 0
        capture_count_lock = threading.Lock()
        selection_entered = threading.Event()
        release_selection = threading.Event()
        force_done = threading.Event()

        def paused_second_capture():
            nonlocal capture_count
            with capture_count_lock:
                capture_count += 1
                this_capture = capture_count
            if this_capture == 2:
                selection_entered.set()
                assert release_selection.wait(2)
            return real_capture()

        def force_home_b():
            token_b = set_hermes_home_override(home_b)
            try:
                manager.discover_and_load(force=True)
            finally:
                reset_hermes_home_override(token_b)
                force_done.set()

        monkeypatch.setattr(
            plugins_mod,
            "_capture_required_policy_runtime_snapshot",
            paused_second_capture,
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            authorization = executor.submit(
                copy_context().run,
                authorize_required_tool_policies,
                _runner_input("home-a"),
            )
            assert selection_entered.wait(1)
            forced = executor.submit(copy_context().run, force_home_b)
            force_finished_before_selection = force_done.wait(0.5)
            release_selection.set()
            block = authorization.result(timeout=2)
            forced.result(timeout=2)

        assert force_finished_before_selection is False
        assert block is not None
        assert block.message == "home-a-policy"
    finally:
        reset_hermes_home_override(token_a)


def test_authorize_capture_cannot_mix_same_home_force_generation(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    token = set_hermes_home_override(home)
    try:
        manager = _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        plugin_dir = _write_custom_required_policy_recovery_plugin(home / "plugins")
        _write_required_policy_runtime_source(
            plugin_dir,
            hook_value="old-hook",
            policy_message="old-policy",
        )
        _write_required_policy_recovery_config(home)
        assert plugins_mod.recover_required_policy_plugins() is True
        _write_required_policy_runtime_source(
            plugin_dir,
            hook_value="new-hook",
            policy_message="new-policy",
        )

        real_capture = plugins_mod._capture_required_policy_runtime_snapshot
        capture_count = 0
        selection_entered = threading.Event()
        release_selection = threading.Event()
        force_done = threading.Event()

        def paused_second_capture():
            nonlocal capture_count
            capture_count += 1
            if capture_count == 2:
                selection_entered.set()
                assert release_selection.wait(2)
            return real_capture()

        def force_rediscover():
            try:
                manager.discover_and_load(force=True)
            finally:
                force_done.set()

        monkeypatch.setattr(
            plugins_mod,
            "_capture_required_policy_runtime_snapshot",
            paused_second_capture,
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            authorization = executor.submit(
                copy_context().run,
                authorize_required_tool_policies,
                _runner_input("same-home"),
            )
            assert selection_entered.wait(1)
            forced = executor.submit(copy_context().run, force_rediscover)
            force_finished_before_selection = force_done.wait(0.5)
            release_selection.set()
            block = authorization.result(timeout=2)
            forced.result(timeout=2)

        assert force_finished_before_selection is False
        assert block is not None
        assert block.message in {"old-policy", "new-policy"}
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize("candidate_shape", ["model-provider", "declared-tool"])
def test_required_policy_recovery_rejects_non_policy_only_candidate_before_import(
    tmp_path,
    monkeypatch,
    candidate_shape,
):
    from tools.registry import registry

    home = tmp_path / f"home-{candidate_shape}"
    marker = tmp_path / f"imported-{candidate_shape}.txt"
    token = set_hermes_home_override(home)
    try:
        _empty_required_policy_recovery_manager(home, tmp_path, monkeypatch)
        plugin_dir = home / "plugins" / "late-governor"
        plugin_dir.mkdir(parents=True)
        manifest = {
            "name": "late-governor",
            "version": "1.0.0",
            "policies": ["tool_dispatch"],
        }
        if candidate_shape == "model-provider":
            manifest["kind"] = "model-provider"
        else:
            manifest["provides_tools"] = ["late_tool"]
        (plugin_dir / "plugin.yaml").write_text(
            yaml.safe_dump(manifest),
            encoding="utf-8",
        )
        (plugin_dir / "__init__.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('imported', encoding='utf-8')\n"
            "def policy(payload):\n"
            "    return {'action': 'allow', 'policy_binding': payload['policy_binding']}\n"
            "def register(ctx):\n"
            "    ctx.register_policy('tool_dispatch', policy)\n",
            encoding="utf-8",
        )
        _write_required_policy_recovery_config(home)
        snapshot_generation = (
            plugins_mod._capture_required_policy_runtime_snapshot().generation
        )
        registry_generation = registry._generation

        assert plugins_mod.recover_required_policy_plugins() is False
        assert not marker.exists()
        assert (
            plugins_mod._capture_required_policy_runtime_snapshot().generation
            == snapshot_generation
        )
        assert registry._generation == registry_generation
    finally:
        reset_hermes_home_override(token)
