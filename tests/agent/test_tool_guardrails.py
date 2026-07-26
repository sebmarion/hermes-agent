"""Pure tool-call guardrail primitive tests."""

import json

from agent.tool_guardrails import (
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
    ToolCallObservation,
    ToolCallSignature,
    canonical_tool_args,
    classify_tool_failure,
)


def test_tool_call_signature_hashes_canonical_nested_unicode_args_without_exposing_raw_args():
    args_a = {
        "z": [{"β": "☤", "a": 1}],
        "a": {"y": 2, "x": "secret-token-value"},
    }
    args_b = {
        "a": {"x": "secret-token-value", "y": 2},
        "z": [{"a": 1, "β": "☤"}],
    }

    assert canonical_tool_args(args_a) == canonical_tool_args(args_b)
    sig_a = ToolCallSignature.from_call("web_search", args_a)
    sig_b = ToolCallSignature.from_call("web_search", args_b)

    assert sig_a == sig_b
    assert len(sig_a.args_hash) == 64
    metadata = sig_a.to_metadata()
    assert metadata == {"tool_name": "web_search", "args_hash": sig_a.args_hash}
    assert "secret-token-value" not in json.dumps(metadata)
    assert "☤" not in json.dumps(metadata)


def test_default_config_is_soft_warning_only_with_hard_stop_disabled():
    cfg = ToolCallGuardrailConfig()

    assert cfg.warnings_enabled is True
    assert cfg.hard_stop_enabled is False
    assert cfg.exact_failure_warn_after == 2
    assert cfg.same_tool_failure_warn_after == 3
    assert cfg.no_progress_warn_after == 2
    assert cfg.exact_failure_block_after == 5
    assert cfg.same_tool_failure_halt_after == 8
    assert cfg.no_progress_block_after == 5


def test_config_parses_nested_warn_and_hard_stop_thresholds():
    cfg = ToolCallGuardrailConfig.from_mapping(
        {
            "warnings_enabled": False,
            "hard_stop_enabled": True,
            "warn_after": {
                "exact_failure": 3,
                "same_tool_failure": 4,
                "idempotent_no_progress": 5,
            },
            "hard_stop_after": {
                "exact_failure": 6,
                "same_tool_failure": 7,
                "idempotent_no_progress": 8,
            },
        }
    )

    assert cfg.warnings_enabled is False
    assert cfg.hard_stop_enabled is True
    assert cfg.exact_failure_warn_after == 3
    assert cfg.same_tool_failure_warn_after == 4
    assert cfg.no_progress_warn_after == 5
    assert cfg.exact_failure_block_after == 6
    assert cfg.same_tool_failure_halt_after == 7
    assert cfg.no_progress_block_after == 8


def test_default_repeated_identical_failed_call_warns_without_blocking():
    controller = ToolCallGuardrailController()
    args = {"query": "same"}

    decisions = []
    for _ in range(5):
        assert controller.before_call("web_search", args).action == "allow"
        decisions.append(
            controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
        )

    assert decisions[0].action == "allow"
    assert [d.action for d in decisions[1:]] == ["warn", "warn", "warn", "warn"]
    assert {d.code for d in decisions[1:]} == {"repeated_exact_failure_warning"}
    assert controller.before_call("web_search", args).action == "allow"
    assert controller.halt_decision is None


def test_hard_stop_enabled_blocks_repeated_exact_failure_before_next_execution():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_warn_after=2,
            exact_failure_block_after=2,
            same_tool_failure_halt_after=99,
        )
    )
    args = {"query": "same"}

    assert controller.before_call("web_search", args).action == "allow"
    first = controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    assert first.action == "allow"

    assert controller.before_call("web_search", args).action == "allow"
    second = controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    assert second.action == "warn"
    assert second.code == "repeated_exact_failure_warning"

    blocked = controller.before_call("web_search", args)
    assert blocked.action == "block"
    assert blocked.code == "repeated_exact_failure_block"
    assert blocked.count == 2


def test_success_resets_exact_signature_failure_streak():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True, exact_failure_block_after=2, same_tool_failure_halt_after=99)
    )
    args = {"query": "same"}

    controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    controller.after_call("web_search", args, '{"ok":true}', failed=False)

    assert controller.before_call("web_search", args).action == "allow"
    controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    assert controller.before_call("web_search", args).action == "allow"


def test_file_mutation_lint_error_result_is_not_a_tool_failure():
    write_result = json.dumps({
        "bytes_written": 12,
        "lint": {"status": "error", "output": "SyntaxError: invalid syntax"},
    })
    patch_result = json.dumps({
        "success": True,
        "diff": "--- a/tmp.py\n+++ b/tmp.py\n",
        "lsp_diagnostics": "<diagnostics>ERROR [1:1] type mismatch</diagnostics>",
    })

    assert classify_tool_failure("write_file", write_result) == (False, "")
    assert classify_tool_failure("patch", patch_result) == (False, "")


def test_same_tool_varying_args_warns_by_default_without_halting():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(same_tool_failure_warn_after=2, same_tool_failure_halt_after=3)
    )

    first = controller.after_call("terminal", {"command": "cmd-1"}, '{"exit_code":1}', failed=True)
    second = controller.after_call("terminal", {"command": "cmd-2"}, '{"exit_code":1}', failed=True)
    third = controller.after_call("terminal", {"command": "cmd-3"}, '{"exit_code":1}', failed=True)
    fourth = controller.after_call("terminal", {"command": "cmd-4"}, '{"exit_code":1}', failed=True)

    assert first.action == "allow"
    assert [second.action, third.action, fourth.action] == ["warn", "warn", "warn"]
    assert {second.code, third.code, fourth.code} == {"same_tool_failure_warning"}
    assert "Do not switch to text-only replies" in second.message
    assert "keep using tools" in second.message
    assert "diagnose before retrying" in second.message
    assert "different tool" in second.message
    assert controller.halt_decision is None


def test_hard_stop_enabled_halts_same_tool_varying_args_failure_streak():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_block_after=99,
            same_tool_failure_warn_after=2,
            same_tool_failure_halt_after=3,
        )
    )

    first = controller.after_call("terminal", {"command": "cmd-1"}, '{"exit_code":1}', failed=True)
    assert first.action == "allow"
    second = controller.after_call("terminal", {"command": "cmd-2"}, '{"exit_code":1}', failed=True)
    assert second.action == "warn"
    assert second.code == "same_tool_failure_warning"
    third = controller.after_call("terminal", {"command": "cmd-3"}, '{"exit_code":1}', failed=True)
    assert third.action == "halt"
    assert third.code == "same_tool_failure_halt"
    assert third.count == 3


def test_parallel_failures_in_one_assistant_batch_count_as_one_epoch():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_warn_after=99,
            exact_failure_block_after=99,
            same_tool_failure_warn_after=99,
            same_tool_failure_halt_after=2,
        )
    )
    observations = [
        ToolCallObservation(
            "session_search",
            {"query": f"q-{index}"},
            '{"error":"boom"}',
            failed=True,
        )
        for index in range(4)
    ]

    first = controller.after_batch(observations)

    assert len(first) == 4
    assert {decision.count for decision in first} == {1}
    assert controller.raw_call_counts == {"session_search": 4}
    assert controller.halt_decision is None

    second = controller.after_batch(observations)

    assert {decision.code for decision in second} == {"same_tool_failure_halt"}
    assert {decision.count for decision in second} == {2}
    assert controller.raw_call_counts == {"session_search": 8}
    assert controller.halt_decision is not None
    assert controller.halt_decision.count == 2


def test_parallel_exact_signature_failures_increment_once_per_epoch():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_warn_after=99,
            exact_failure_block_after=2,
            same_tool_failure_halt_after=99,
        )
    )
    observation = ToolCallObservation(
        "session_search",
        {"query": "same"},
        '{"error":"boom"}',
        failed=True,
    )

    controller.after_batch([observation, observation, observation, observation])
    assert controller.before_call("session_search", {"query": "same"}).action == "allow"

    controller.after_batch([observation, observation])
    blocked = controller.before_call("session_search", {"query": "same"})
    assert blocked.code == "repeated_exact_failure_block"
    assert blocked.count == 2


def test_no_progress_result_set_is_independent_of_worker_completion_order():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(no_progress_warn_after=2)
    )
    args = {"query": "same"}
    first = [
        ToolCallObservation("web_search", args, '{"value":"a"}', failed=False),
        ToolCallObservation("web_search", args, '{"value":"b"}', failed=False),
    ]
    second = list(reversed(first))

    assert {decision.action for decision in controller.after_batch(first)} == {"allow"}
    decisions = controller.after_batch(second)

    assert {decision.code for decision in decisions} == {"idempotent_no_progress_warning"}
    assert {decision.count for decision in decisions} == {2}


def test_mixed_success_and_failure_in_one_epoch_resets_failure_and_no_progress_state():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_block_after=2,
            same_tool_failure_halt_after=2,
            no_progress_block_after=2,
        )
    )
    args = {"query": "same"}
    controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    controller.after_call("web_search", args, '{"value":"same"}', failed=False)
    controller.after_batch([
        ToolCallObservation("web_search", args, '{"error":"boom"}', failed=True),
        ToolCallObservation("web_search", args, '{"value":"same"}', failed=False),
    ])

    assert controller.before_call("web_search", args).action == "allow"
    assert controller.halt_decision is None


def _trigger_no_effect_recovery(controller, tool_name="session_search"):
    first = controller.after_call(
        tool_name,
        {"query": "first"},
        '{"error":"boom"}',
        failed=True,
    )
    assert first.should_halt is False
    trigger = controller.after_call(
        tool_name,
        {"query": "second"},
        '{"error":"boom"}',
        failed=True,
    )
    assert trigger.code == "same_tool_failure_halt"
    assert controller.start_recovery(trigger) is True
    return trigger


def test_recovery_pending_blocks_quarantined_and_effect_capable_calls_only():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_warn_after=99,
            exact_failure_block_after=99,
            same_tool_failure_warn_after=99,
            same_tool_failure_halt_after=2,
            no_progress_block_after=99,
        )
    )
    _trigger_no_effect_recovery(controller)

    quarantined = controller.before_call("session_search", {"query": "again"})
    effectful = controller.before_call("terminal", {"command": "pwd"})
    unknown = controller.before_call("mcp_unknown_reader", {"path": "/tmp/x"})
    alternative = controller.before_call("read_file", {"path": "/tmp/x"})

    assert quarantined.code == "recovery_quarantined_tool_block"
    assert quarantined.allows_execution is False
    assert effectful.code == "recovery_effectful_tool_block"
    assert effectful.allows_execution is False
    assert unknown.code == "recovery_effectful_tool_block"
    assert unknown.allows_execution is False
    assert alternative.action == "allow"
    assert controller.halt_decision is None


def test_successful_no_effect_alternative_resolves_recovery_but_keeps_quarantine():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_warn_after=99,
            exact_failure_block_after=99,
            same_tool_failure_warn_after=99,
            same_tool_failure_halt_after=2,
            no_progress_block_after=99,
        )
    )
    _trigger_no_effect_recovery(controller)
    observations = [
        ToolCallObservation(
            "session_search",
            {"query": "again"},
            '{"error":"quarantined"}',
            failed=True,
            executed=False,
        ),
        ToolCallObservation(
            "terminal",
            {"command": "pwd"},
            '{"error":"effectful blocked"}',
            failed=True,
            executed=False,
        ),
        ToolCallObservation(
            "read_file",
            {"path": "/tmp/x"},
            "contents",
            failed=False,
            executed=True,
        ),
    ]

    decisions = controller.after_batch(observations)
    assert controller.finish_recovery_epoch(observations, decisions) is None
    assert controller.recovery_state == "recovered"
    assert controller.raw_call_counts == {"session_search": 2, "read_file": 1}
    assert controller.halt_decision is None
    assert controller.before_call(
        "session_search",
        {"query": "later"},
    ).code == "quarantined_tool_block"


def test_recovery_with_only_quarantined_retry_halts_deterministically():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_block_after=99,
            same_tool_failure_halt_after=2,
            no_progress_block_after=99,
        )
    )
    _trigger_no_effect_recovery(controller)
    observations = [ToolCallObservation(
        "session_search",
        {"query": "again"},
        '{"error":"quarantined"}',
        failed=True,
        executed=False,
    )]

    decisions = controller.after_batch(observations)
    halt = controller.finish_recovery_epoch(observations, decisions)

    assert halt is not None
    assert halt.code == "recovery_quarantined_only_halt"
    assert controller.recovery_state == "failed"


def test_recovery_halts_when_every_safe_alternative_fails():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_block_after=99,
            same_tool_failure_halt_after=2,
            no_progress_block_after=99,
        )
    )
    _trigger_no_effect_recovery(controller)
    observations = [ToolCallObservation(
        "read_file",
        {"path": "/tmp/x"},
        '{"error":"missing"}',
        failed=True,
        executed=True,
    )]

    decisions = controller.after_batch(observations)
    halt = controller.finish_recovery_epoch(observations, decisions)

    assert halt is not None
    assert halt.code == "recovery_alternative_failed_halt"
    assert controller.recovery_state == "failed"


def test_idempotent_no_progress_repeated_result_warns_without_blocking_by_default():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(no_progress_warn_after=2, no_progress_block_after=2)
    )
    args = {"path": "/tmp/same.txt"}
    result = "same file contents"

    for _ in range(4):
        assert controller.before_call("read_file", args).action == "allow"
        decision = controller.after_call("read_file", args, result, failed=False)

    assert decision.action == "warn"
    assert decision.code == "idempotent_no_progress_warning"
    assert controller.before_call("read_file", args).action == "allow"
    assert controller.halt_decision is None


def test_hard_stop_enabled_blocks_idempotent_no_progress_future_repeat():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            no_progress_warn_after=2,
            no_progress_block_after=2,
        )
    )
    args = {"path": "/tmp/same.txt"}
    result = "same file contents"

    assert controller.before_call("read_file", args).action == "allow"
    assert controller.after_call("read_file", args, result, failed=False).action == "allow"
    assert controller.before_call("read_file", args).action == "allow"
    warn = controller.after_call("read_file", args, result, failed=False)
    assert warn.action == "warn"
    assert warn.code == "idempotent_no_progress_warning"

    blocked = controller.before_call("read_file", args)
    assert blocked.action == "block"
    assert blocked.code == "idempotent_no_progress_block"


def test_mutating_or_unknown_tools_are_not_blocked_for_repeated_identical_success_output_by_default():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(no_progress_warn_after=2, no_progress_block_after=2)
    )

    for _ in range(3):
        assert controller.before_call("write_file", {"path": "/tmp/x", "content": "x"}).action == "allow"
        assert controller.after_call("write_file", {"path": "/tmp/x", "content": "x"}, "ok", failed=False).action == "allow"
        assert controller.before_call("custom_tool", {"x": 1}).action == "allow"
        assert controller.after_call("custom_tool", {"x": 1}, "ok", failed=False).action == "allow"


def test_reset_for_turn_clears_bounded_guardrail_state():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True, exact_failure_block_after=2, no_progress_block_after=2)
    )
    controller.after_call("web_search", {"query": "same"}, '{"error":"boom"}', failed=True)
    controller.after_call("web_search", {"query": "same"}, '{"error":"boom"}', failed=True)
    controller.after_call("read_file", {"path": "/tmp/x"}, "same", failed=False)
    controller.after_call("read_file", {"path": "/tmp/x"}, "same", failed=False)

    assert controller.before_call("web_search", {"query": "same"}).action == "block"
    assert controller.before_call("read_file", {"path": "/tmp/x"}).action == "block"

    controller.reset_for_turn()

    assert controller.before_call("web_search", {"query": "same"}).action == "allow"
    assert controller.before_call("read_file", {"path": "/tmp/x"}).action == "allow"
