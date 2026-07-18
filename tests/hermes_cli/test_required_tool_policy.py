import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import FrozenInstanceError, fields
from types import MappingProxyType
from unittest.mock import MagicMock, patch

import pytest

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
    clear_required_policy_quarantine()
    yield
    clear_required_policy_quarantine()
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
