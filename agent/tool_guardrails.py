"""Pure tool-call loop guardrail primitives.

The controller in this module is intentionally side-effect free: it tracks
per-turn tool-call observations and returns decisions. Runtime code owns whether
those decisions become warning guidance, synthetic tool results, or controlled
turn halts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Mapping

from utils import safe_json_loads
from agent.tool_result_classification import (
    file_mutation_result_landed,
    file_mutation_result_proves_change,
    tool_may_have_side_effect,
)


IDEMPOTENT_TOOL_NAMES = frozenset(
    {
        "read_file",
        "search_files",
        "web_search",
        "web_extract",
        "session_search",
        "browser_snapshot",
        "browser_console",
        "browser_get_images",
        "mcp_filesystem_read_file",
        "mcp_filesystem_read_text_file",
        "mcp_filesystem_read_multiple_files",
        "mcp_filesystem_list_directory",
        "mcp_filesystem_list_directory_with_sizes",
        "mcp_filesystem_directory_tree",
        "mcp_filesystem_get_file_info",
        "mcp_filesystem_search_files",
    }
)

MUTATING_TOOL_NAMES = frozenset(
    {
        "terminal",
        "execute_code",
        "write_file",
        "patch",
        "todo",
        "memory",
        "skill_manage",
        "browser_click",
        "browser_type",
        "browser_press",
        "browser_scroll",
        "browser_navigate",
        "send_message",
        "cronjob",
        "delegate_task",
        "process",
    }
)

FAILURE_STREAK_GUARDRAIL_CODES = frozenset({
    "repeated_exact_failure_block",
    "same_tool_failure_halt",
})


@dataclass(frozen=True)
class ToolCallGuardrailConfig:
    """Thresholds for per-turn tool-call loop detection.

    Warnings are enabled by default and never prevent tool execution. Hard stops
    are explicit opt-in so interactive CLI/TUI sessions get a gentle nudge unless
    the user enables circuit-breaker behavior in config.yaml.
    """

    warnings_enabled: bool = True
    hard_stop_enabled: bool = False
    terminal_exact_failure_only: bool = False
    exact_failure_warn_after: int = 2
    exact_failure_block_after: int = 5
    same_tool_failure_warn_after: int = 3
    same_tool_failure_halt_after: int = 8
    no_progress_warn_after: int = 2
    no_progress_block_after: int = 5
    idempotent_tools: frozenset[str] = field(default_factory=lambda: IDEMPOTENT_TOOL_NAMES)
    mutating_tools: frozenset[str] = field(default_factory=lambda: MUTATING_TOOL_NAMES)
    loop_caps: "LoopCapConfig" = field(default_factory=lambda: LoopCapConfig())

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "ToolCallGuardrailConfig":
        """Build config from the `tool_loop_guardrails` config.yaml section."""
        if not isinstance(data, Mapping):
            return cls()

        warn_after = data.get("warn_after")
        if not isinstance(warn_after, Mapping):
            warn_after = {}
        hard_stop_after = data.get("hard_stop_after")
        if not isinstance(hard_stop_after, Mapping):
            hard_stop_after = {}

        defaults = cls()
        return cls(
            warnings_enabled=_as_bool(data.get("warnings_enabled"), defaults.warnings_enabled),
            hard_stop_enabled=_as_bool(data.get("hard_stop_enabled"), defaults.hard_stop_enabled),
            terminal_exact_failure_only=_as_bool(
                data.get("terminal_exact_failure_only"),
                defaults.terminal_exact_failure_only,
            ),
            exact_failure_warn_after=_positive_int(
                warn_after.get("exact_failure", data.get("exact_failure_warn_after")),
                defaults.exact_failure_warn_after,
            ),
            same_tool_failure_warn_after=_positive_int(
                warn_after.get("same_tool_failure", data.get("same_tool_failure_warn_after")),
                defaults.same_tool_failure_warn_after,
            ),
            no_progress_warn_after=_positive_int(
                warn_after.get("idempotent_no_progress", data.get("no_progress_warn_after")),
                defaults.no_progress_warn_after,
            ),
            exact_failure_block_after=_positive_int(
                hard_stop_after.get("exact_failure", data.get("exact_failure_block_after")),
                defaults.exact_failure_block_after,
            ),
            same_tool_failure_halt_after=_positive_int(
                hard_stop_after.get("same_tool_failure", data.get("same_tool_failure_halt_after")),
                defaults.same_tool_failure_halt_after,
            ),
            no_progress_block_after=_positive_int(
                hard_stop_after.get("idempotent_no_progress", data.get("no_progress_block_after")),
                defaults.no_progress_block_after,
            ),
            loop_caps=LoopCapConfig.from_mapping(data.get("loop_caps")),
        )


# Per-turn ceilings for the two tools most likely to create unbounded work.
_DEFAULT_MAX_WEB_SEARCHES_PER_TURN = 50
_DEFAULT_MAX_SUBAGENTS_PER_TURN = 50


@dataclass(frozen=True)
class LoopCapConfig:
    """Hard per-turn caps for runaway search and delegation loops."""

    max_web_searches: int = _DEFAULT_MAX_WEB_SEARCHES_PER_TURN
    max_subagents: int = _DEFAULT_MAX_SUBAGENTS_PER_TURN

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "LoopCapConfig":
        if not isinstance(data, Mapping):
            return cls()
        defaults = cls()
        return cls(
            max_web_searches=_non_negative_int(
                data.get("max_web_searches"), defaults.max_web_searches
            ),
            max_subagents=_non_negative_int(
                data.get("max_subagents"), defaults.max_subagents
            ),
        )


@dataclass(frozen=True)
class ToolCallSignature:
    """Stable, non-reversible identity for a tool name plus canonical args."""

    tool_name: str
    args_hash: str

    @classmethod
    def from_call(cls, tool_name: str, args: Mapping[str, Any] | None) -> "ToolCallSignature":
        canonical = canonical_tool_args(args or {})
        return cls(tool_name=tool_name, args_hash=_sha256(canonical))

    def to_metadata(self) -> dict[str, str]:
        """Return public metadata without raw argument values."""
        return {"tool_name": self.tool_name, "args_hash": self.args_hash}


@dataclass(frozen=True)
class ToolCallObservation:
    """A model-visible tool result tied to its guardrail authorization identity."""

    tool_name: str
    args: Mapping[str, Any] | None
    result: str | None
    failed: bool | None = None
    executed: bool = True
    guardrail_signature: ToolCallSignature | None = None


@dataclass(frozen=True)
class ToolGuardrailDecision:
    """Decision returned by the tool-call guardrail controller."""

    action: str = "allow"  # allow | warn | deny | block | halt
    code: str = "allow"
    message: str = ""
    tool_name: str = ""
    count: int = 0
    signature: ToolCallSignature | None = None
    exact_count: int | None = None
    broad_count: int | None = None

    @property
    def allows_execution(self) -> bool:
        return self.action in {"allow", "warn"}

    @property
    def should_halt(self) -> bool:
        return self.action in {"block", "halt"}

    def to_metadata(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "action": self.action,
            "code": self.code,
            "message": self.message,
            "tool_name": self.tool_name,
            "count": self.count,
        }
        if self.signature is not None:
            data["signature"] = self.signature.to_metadata()
        if self.exact_count is not None:
            data["exact_count"] = self.exact_count
        if self.broad_count is not None:
            data["broad_count"] = self.broad_count
        return data


def canonical_tool_args(args: Mapping[str, Any]) -> str:
    """Return sorted compact JSON for parsed tool arguments."""
    if not isinstance(args, Mapping):
        raise TypeError(f"tool args must be a mapping, got {type(args).__name__}")
    return json.dumps(
        args,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def classify_tool_failure(tool_name: str, result: str | None) -> tuple[bool, str]:
    """Safety-fallback classifier used only when callers don't pass ``failed``.

    Mirrors ``agent.display._detect_tool_failure`` exactly so the guardrail
    never disagrees with the CLI's user-visible ``[error]`` tag. Production
    callers in ``run_agent.py`` always pass an explicit ``failed=`` derived
    from ``_detect_tool_failure``; this function exists so standalone callers
    (tests, tooling) still get consistent behavior.
    """
    if result is None:
        return False, ""
    if file_mutation_result_landed(tool_name, result):
        return False, ""

    if tool_name == "terminal":
        data = safe_json_loads(result)
        if isinstance(data, dict):
            exit_code = data.get("exit_code")
            if exit_code is not None and exit_code != 0:
                return True, f" [exit {exit_code}]"
        return False, ""

    if tool_name == "memory":
        data = safe_json_loads(result)
        if isinstance(data, dict):
            if data.get("success") is False and "exceed the limit" in data.get("error", ""):
                return True, " [full]"

    lower = result[:500].lower()
    if '"error"' in lower or '"failed"' in lower or result.startswith("Error"):
        return True, " [error]"

    return False, ""


def observation_proves_verified_file_progress(
    observation: ToolCallObservation,
) -> bool:
    """Return True for an executed successful patch with a material diff."""
    if not observation.executed:
        return False
    failed = observation.failed
    if failed is None:
        failed, _ = classify_tool_failure(
            observation.tool_name,
            observation.result,
        )
    return not failed and file_mutation_result_proves_change(
        observation.tool_name,
        observation.result,
    )


class ToolCallGuardrailController:
    """Per-turn controller for repeated failed/non-progressing tool calls."""

    def __init__(self, config: ToolCallGuardrailConfig | None = None):
        self.config = config or ToolCallGuardrailConfig()
        self._state_lock = RLock()
        self.reset_for_turn()

    def reset_for_turn(self) -> None:
        self._exact_failure_counts: dict[ToolCallSignature, int] = {}
        self._same_tool_failure_counts: dict[str, int] = {}
        self._no_progress: dict[ToolCallSignature, tuple[str, int]] = {}
        self._typed_permanent_by_tool: dict[str, tuple[str, str]] = {}
        self._typed_permanent_by_signature: dict[
            ToolCallSignature,
            tuple[str, str],
        ] = {}
        self._schema_retry_policies: dict[tuple[str, str], dict[str, Any]] = {}
        self._raw_call_counts: dict[str, int] = {}
        self._observation_epochs = 0
        self._recovery_state = "normal"
        self._recovery_tool: str | None = None
        self._recovery_trigger: ToolGuardrailDecision | None = None
        self._halt_decision: ToolGuardrailDecision | None = None
        self._turn_web_search_count = 0
        self._turn_subagent_count = 0

    @property
    def raw_call_counts(self) -> dict[str, int]:
        return dict(self._raw_call_counts)

    @property
    def observation_epochs(self) -> int:
        return self._observation_epochs

    @property
    def recovery_state(self) -> str:
        return self._recovery_state

    @property
    def recovery_tool(self) -> str | None:
        return self._recovery_tool

    def start_recovery(self, decision: ToolGuardrailDecision) -> bool:
        """Quarantine one proven no-effect tool for exactly one pivot epoch."""
        if (
            self._recovery_state != "normal"
            or not decision.should_halt
            or decision.code != "same_tool_failure_halt"
            or not decision.tool_name
            or tool_may_have_side_effect(decision.tool_name)
        ):
            return False
        self._recovery_state = "pending"
        self._recovery_tool = decision.tool_name
        self._recovery_trigger = decision
        self._halt_decision = None
        return True

    def finish_recovery_epoch(
        self,
        observations: list[ToolCallObservation] | tuple[ToolCallObservation, ...],
        decisions: list[ToolGuardrailDecision] | tuple[ToolGuardrailDecision, ...],
    ) -> ToolGuardrailDecision | None:
        """Resolve a pending pivot after its complete assistant batch."""
        if self._recovery_state != "pending":
            return None

        for decision in decisions:
            if decision.should_halt:
                self._recovery_state = "failed"
                self._halt_decision = decision
                return decision

        alternatives: list[tuple[ToolCallObservation, bool]] = []
        for observation in observations:
            if (
                not observation.executed
                or observation.tool_name == self._recovery_tool
                or tool_may_have_side_effect(observation.tool_name)
            ):
                continue
            failed = observation.failed
            if failed is None:
                failed, _ = classify_tool_failure(
                    observation.tool_name,
                    observation.result,
                )
            alternatives.append((observation, bool(failed)))

        if any(not failed for _observation, failed in alternatives):
            self._recovery_state = "recovered"
            self._halt_decision = None
            return None

        only_quarantined = bool(observations) and all(
            observation.tool_name == self._recovery_tool
            for observation in observations
        )
        if only_quarantined:
            code = "recovery_quarantined_only_halt"
            message = (
                f"Stopped recovery: {self._recovery_tool} is quarantined for "
                "this turn and the model retried only that tool."
            )
        elif alternatives:
            code = "recovery_alternative_failed_halt"
            message = (
                f"Stopped recovery for {self._recovery_tool}: every safe "
                "no-effect alternative failed."
            )
        else:
            code = "recovery_no_safe_alternative_halt"
            message = (
                f"Stopped recovery for {self._recovery_tool}: the model did "
                "not provide an executable no-effect alternative."
            )
        trigger = self._recovery_trigger
        decision = ToolGuardrailDecision(
            action="halt",
            code=code,
            message=message,
            tool_name=self._recovery_tool or "",
            count=trigger.count if trigger is not None else 0,
            signature=trigger.signature if trigger is not None else None,
        )
        self._recovery_state = "failed"
        self._halt_decision = decision
        return decision

    def resolve_recovery_with_final_text(self) -> bool:
        """Treat a visible no-tool assistant response as a successful pivot."""
        if self._recovery_state != "pending":
            return False
        self._recovery_state = "recovered"
        self._halt_decision = None
        return True

    @property
    def halt_decision(self) -> ToolGuardrailDecision | None:
        return self._halt_decision

    def _typed_retry_before_call(
        self,
        tool_name: str,
        signature: ToolCallSignature,
    ) -> ToolGuardrailDecision | None:
        """Authorize one typed/schema retry and reserve it until observation."""
        with self._state_lock:
            permanent = self._typed_permanent_by_tool.get(tool_name)
            if permanent is None:
                permanent = self._typed_permanent_by_signature.get(signature)
            if permanent is not None:
                error_code, error_class = permanent
                return ToolGuardrailDecision(
                    action="deny",
                    code="typed_permanent_failure_no_retry",
                    message=(
                        f"Denied {tool_name}: its prior {error_class} failure "
                        f"({error_code}) is non-retryable in this turn. Change "
                        "capability, credentials, permissions, or strategy instead."
                    ),
                    tool_name=tool_name,
                    count=1,
                    signature=signature,
                )

            for (policy_tool, error_code), policy in (
                self._schema_retry_policies.items()
            ):
                if policy_tool != tool_name:
                    continue
                failed_signatures = policy["failed_signatures"]
                if signature in failed_signatures:
                    if policy["requires_argument_change"]:
                        return ToolGuardrailDecision(
                            action="deny",
                            code="schema_retry_requires_changed_arguments",
                            message=(
                                f"Denied {tool_name}: retrying the same arguments "
                                f"cannot correct schema failure {error_code}. "
                                "Apply the typed correction before retrying."
                            ),
                            tool_name=tool_name,
                            count=1,
                            signature=signature,
                        )
                    remediation = policy.get("remediation_signature")
                    if (
                        remediation is not None
                        and not policy["remediation_satisfied"]
                    ):
                        return ToolGuardrailDecision(
                            action="deny",
                            code="schema_retry_requires_remediation",
                            message=(
                                f"Denied {tool_name}: retrying schema failure "
                                f"{error_code} requires one successful "
                                f"{remediation.tool_name} call with the requested "
                                "arguments first."
                            ),
                            tool_name=tool_name,
                            count=1,
                            signature=signature,
                        )
                elif not policy["requires_argument_change"]:
                    # A different call is unrelated to this external-precondition
                    # retry and must not consume its one allowed attempt.
                    continue
                if policy["corrected_retry_used"]:
                    return ToolGuardrailDecision(
                        action="deny",
                        code="schema_corrected_retry_exhausted",
                        message=(
                            f"Denied {tool_name}: the single corrected retry for "
                            f"schema failure {error_code} was already used. Stop "
                            "guessing and choose a different strategy."
                        ),
                        tool_name=tool_name,
                        count=2,
                        signature=signature,
                    )
                if policy.get("corrected_retry_pending") is not None:
                    return ToolGuardrailDecision(
                        action="deny",
                        code="schema_corrected_retry_reserved",
                        message=(
                            f"Denied {tool_name}: the single corrected retry for "
                            f"schema failure {error_code} is already reserved by "
                            "another call awaiting an execution result."
                        ),
                        tool_name=tool_name,
                        count=1,
                        signature=signature,
                    )
                policy["corrected_retry_pending"] = signature
                break
        return None

    def _record_typed_observations(
        self,
        normalized: list[
            tuple[ToolCallObservation, ToolCallSignature, bool]
        ],
    ) -> None:
        """Commit typed retry state from one model-ordered observation batch."""
        with self._state_lock:
            for (policy_tool, _error_code), policy in (
                self._schema_retry_policies.items()
            ):
                pending = policy.get("corrected_retry_pending")
                if pending is None:
                    continue
                matches = [
                    observation
                    for observation, signature, _failed in normalized
                    if observation.tool_name == policy_tool and signature == pending
                ]
                if not matches:
                    continue
                if any(observation.executed for observation in matches):
                    policy["corrected_retry_used"] = True
                policy["corrected_retry_pending"] = None

            for observation, signature, failed in normalized:
                if not observation.executed:
                    continue
                if failed:
                    error_class, error_code = _typed_tool_error_policy(
                        observation.result,
                    )
                    if error_class in {"auth", "capability", "permanent"}:
                        self._typed_permanent_by_tool[observation.tool_name] = (
                            error_code,
                            error_class,
                        )
                    elif error_class == "permission":
                        self._typed_permanent_by_signature[signature] = (
                            error_code,
                            error_class,
                        )
                    elif error_class == "schema_correctable":
                        retry_policy = _typed_schema_retry_policy(
                            observation.result,
                        )
                        policy = self._schema_retry_policies.setdefault(
                            (observation.tool_name, error_code),
                            {
                                "failed_signatures": set(),
                                "corrected_retry_used": False,
                                "corrected_retry_pending": None,
                                **retry_policy,
                            },
                        )
                        policy["failed_signatures"].add(signature)
                    continue

                for policy in self._schema_retry_policies.values():
                    if policy.get("remediation_signature") == signature:
                        policy["remediation_satisfied"] = True
                for key in [
                    key
                    for key in self._schema_retry_policies
                    if key[0] == observation.tool_name
                ]:
                    self._schema_retry_policies.pop(key, None)

    def before_call(self, tool_name: str, args: Mapping[str, Any] | None) -> ToolGuardrailDecision:
        signature = ToolCallSignature.from_call(tool_name, _coerce_args(args))
        if self._recovery_tool and tool_name == self._recovery_tool:
            pending = self._recovery_state == "pending"
            decision = ToolGuardrailDecision(
                action="block",
                code=(
                    "recovery_quarantined_tool_block"
                    if pending
                    else "quarantined_tool_block"
                ),
                message=(
                    f"Blocked {tool_name}: it is quarantined for this turn after "
                    "repeated non-progress. Use a different no-effect tool or the "
                    "evidence already available."
                ),
                tool_name=tool_name,
                count=self._recovery_trigger.count if self._recovery_trigger else 0,
                signature=signature,
            )
            if not pending:
                self._halt_decision = decision
            return decision

        if self._recovery_state == "pending" and tool_may_have_side_effect(tool_name):
            return ToolGuardrailDecision(
                action="block",
                code="recovery_effectful_tool_block",
                message=(
                    f"Blocked {tool_name} during guardrail recovery because it "
                    "may have side effects. Recovery may execute only known "
                    "no-effect tools."
                ),
                tool_name=tool_name,
                signature=signature,
            )

        cap_block = self._check_loop_cap(tool_name, _coerce_args(args), signature)
        if cap_block is not None:
            return cap_block

        typed_retry_decision = self._typed_retry_before_call(tool_name, signature)
        if typed_retry_decision is not None:
            return typed_retry_decision

        if not self.config.hard_stop_enabled:
            return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

        exact_count = self._exact_failure_counts.get(signature, 0)
        if exact_count >= self.config.exact_failure_block_after:
            broad_count = self._same_tool_failure_counts.get(tool_name, 0)
            decision = ToolGuardrailDecision(
                action="block",
                code="repeated_exact_failure_block",
                message=(
                    f"Blocked {tool_name}: the same tool call failed {exact_count} "
                    "times with identical arguments. Stop retrying it unchanged; "
                    "change strategy or explain the blocker."
                ),
                tool_name=tool_name,
                count=exact_count,
                signature=signature,
                exact_count=exact_count,
                broad_count=broad_count,
            )
            self._halt_decision = decision
            return decision

        if self._is_idempotent(tool_name):
            record = self._no_progress.get(signature)
            if record is not None:
                _result_hash, repeat_count = record
                if repeat_count >= self.config.no_progress_block_after:
                    decision = ToolGuardrailDecision(
                        action="block",
                        code="idempotent_no_progress_block",
                        message=(
                            f"Blocked {tool_name}: this read-only call returned the same "
                            f"result {repeat_count} times. Stop repeating it unchanged; "
                            "use the result already provided or try a different query."
                        ),
                        tool_name=tool_name,
                        count=repeat_count,
                        signature=signature,
                    )
                    self._halt_decision = decision
                    return decision

        return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

    def recovery_malformed_arguments_block(
        self,
        tool_name: str,
    ) -> ToolGuardrailDecision:
        signature = ToolCallSignature.from_call(tool_name, {})
        return ToolGuardrailDecision(
            action="block",
            code="recovery_malformed_arguments_block",
            signature=signature,
            tool_name=tool_name,
            count=1,
            message=(
                f"Blocked malformed arguments for {tool_name} during bounded "
                "guardrail recovery. The tool was not executed."
            ),
        )

    def after_call(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None,
        result: str | None,
        *,
        failed: bool | None = None,
    ) -> ToolGuardrailDecision:
        """Compatibility wrapper treating one call as one assistant epoch."""
        return self.after_batch([
            ToolCallObservation(
                tool_name=tool_name,
                args=args,
                result=result,
                failed=failed,
                executed=True,
            )
        ])[0]

    def after_batch(
        self,
        observations: list[ToolCallObservation] | tuple[ToolCallObservation, ...],
    ) -> list[ToolGuardrailDecision]:
        """Finalize one complete assistant tool batch in assistant call order.

        Failure and no-progress streaks advance at most once per signature/tool
        in this epoch. Synthetic policy or guardrail results pass
        ``executed=False`` and remain visible in raw transcripts without being
        mistaken for another failed execution.
        """
        normalized: list[tuple[ToolCallObservation, ToolCallSignature, bool]] = []
        for observation in observations:
            args = _coerce_args(observation.args)
            signature = (
                observation.guardrail_signature
                or ToolCallSignature.from_call(observation.tool_name, args)
            )
            failed = observation.failed
            if failed is None:
                failed, _ = classify_tool_failure(
                    observation.tool_name,
                    observation.result,
                )
            normalized.append((observation, signature, bool(failed)))
            if observation.executed:
                self._raw_call_counts[observation.tool_name] = (
                    self._raw_call_counts.get(observation.tool_name, 0) + 1
                )

        executed = [item for item in normalized if item[0].executed]
        self._record_typed_observations(normalized)

        if any(
            observation_proves_verified_file_progress(observation)
            for observation, _signature, _failed in executed
        ):
            # A verified material patch establishes a new failure epoch. Clear
            # only historical execution-failure streaks; no-progress, schema,
            # recovery, raw-call, and epoch state remain authoritative.
            self._exact_failure_counts.clear()
            self._same_tool_failure_counts.clear()
            if (
                self._halt_decision is not None
                and self._halt_decision.code in FAILURE_STREAK_GUARDRAIL_CODES
            ):
                self._halt_decision = None
        if executed:
            self._observation_epochs += 1

        by_signature: dict[
            ToolCallSignature,
            list[tuple[ToolCallObservation, bool]],
        ] = {}
        by_tool: dict[str, list[bool]] = {}
        for observation, signature, failed in executed:
            by_signature.setdefault(signature, []).append((observation, failed))
            by_tool.setdefault(observation.tool_name, []).append(failed)

        for signature, signature_observations in by_signature.items():
            failures = [failed for _observation, failed in signature_observations]
            any_success = any(not failed for failed in failures)
            any_failure = any(failures)
            if any_success:
                self._exact_failure_counts.pop(signature, None)
            elif any_failure:
                self._exact_failure_counts[signature] = (
                    self._exact_failure_counts.get(signature, 0) + 1
                )

            if not self._is_idempotent(signature.tool_name):
                self._no_progress.pop(signature, None)
                continue

            if any_success and any_failure:
                self._no_progress.pop(signature, None)
            elif any_failure:
                self._no_progress.pop(signature, None)
            elif signature_observations:
                result_hash = _result_set_hash(
                    observation.result
                    for observation, _failed in signature_observations
                )
                previous = self._no_progress.get(signature)
                repeat_count = 1
                if previous is not None and previous[0] == result_hash:
                    repeat_count = previous[1] + 1
                self._no_progress[signature] = (result_hash, repeat_count)

        for tool_name, failures in by_tool.items():
            if any(not failed for failed in failures):
                self._same_tool_failure_counts.pop(tool_name, None)
            elif any(failures):
                self._same_tool_failure_counts[tool_name] = (
                    self._same_tool_failure_counts.get(tool_name, 0) + 1
                )

        decisions: list[ToolGuardrailDecision] = []
        for observation, signature, failed in normalized:
            if not observation.executed:
                decisions.append(ToolGuardrailDecision(
                    tool_name=observation.tool_name,
                    signature=signature,
                ))
                continue

            if failed:
                exact_count = self._exact_failure_counts.get(signature, 0)
                same_count = self._same_tool_failure_counts.get(
                    observation.tool_name,
                    0,
                )
                if (
                    self.config.hard_stop_enabled
                    and self._same_tool_halt_applies(observation.tool_name)
                    and same_count >= self.config.same_tool_failure_halt_after
                ):
                    decision = ToolGuardrailDecision(
                        action="halt",
                        code="same_tool_failure_halt",
                        message=(
                            f"Stopped {observation.tool_name}: it failed {same_count} "
                            "assistant tool batches this turn. Stop retrying the "
                            "same failing tool path and choose a different approach."
                        ),
                        tool_name=observation.tool_name,
                        count=same_count,
                        signature=signature,
                        exact_count=exact_count,
                        broad_count=same_count,
                    )
                    if self._halt_decision is None:
                        self._halt_decision = decision
                    decisions.append(decision)
                    continue

                if (
                    self.config.warnings_enabled
                    and exact_count >= self.config.exact_failure_warn_after
                ):
                    decisions.append(ToolGuardrailDecision(
                        action="warn",
                        code="repeated_exact_failure_warning",
                        message=(
                            f"{observation.tool_name} has failed {exact_count} "
                            "assistant tool batches with identical arguments. "
                            "This looks like a loop; inspect the error and change "
                            "strategy instead of retrying it unchanged."
                        ),
                        tool_name=observation.tool_name,
                        count=exact_count,
                        signature=signature,
                        exact_count=exact_count,
                        broad_count=same_count,
                    ))
                    continue

                if (
                    self.config.warnings_enabled
                    and same_count >= self.config.same_tool_failure_warn_after
                ):
                    decisions.append(ToolGuardrailDecision(
                        action="warn",
                        code="same_tool_failure_warning",
                        message=_tool_failure_recovery_hint(
                            observation.tool_name,
                            same_count,
                        ),
                        tool_name=observation.tool_name,
                        count=same_count,
                        signature=signature,
                        exact_count=exact_count,
                        broad_count=same_count,
                    ))
                    continue

                decisions.append(ToolGuardrailDecision(
                    tool_name=observation.tool_name,
                    count=exact_count,
                    signature=signature,
                    exact_count=exact_count,
                    broad_count=same_count,
                ))
                continue

            self._exact_failure_counts.pop(signature, None)
            if not self._is_idempotent(observation.tool_name):
                decisions.append(ToolGuardrailDecision(
                    tool_name=observation.tool_name,
                    signature=signature,
                ))
                continue

            _result_hash_value, repeat_count = self._no_progress.get(
                signature,
                ("", 0),
            )
            if (
                self.config.warnings_enabled
                and repeat_count >= self.config.no_progress_warn_after
            ):
                decisions.append(ToolGuardrailDecision(
                    action="warn",
                    code="idempotent_no_progress_warning",
                    message=(
                        f"{observation.tool_name} returned the same result set "
                        f"for {repeat_count} assistant tool batches. Use the "
                        "result already provided or change the query instead of "
                        "repeating it unchanged."
                    ),
                    tool_name=observation.tool_name,
                    count=repeat_count,
                    signature=signature,
                ))
            else:
                decisions.append(ToolGuardrailDecision(
                    tool_name=observation.tool_name,
                    count=repeat_count,
                    signature=signature,
                ))

        return decisions

    def _is_idempotent(self, tool_name: str) -> bool:
        if tool_name in self.config.mutating_tools:
            return False
        return tool_name in self.config.idempotent_tools

    def _check_loop_cap(
        self,
        tool_name: str,
        args: Mapping[str, Any],
        signature: ToolCallSignature,
    ) -> ToolGuardrailDecision | None:
        """Refuse the next runaway-prone call once its per-turn cap is reached."""
        caps = self.config.loop_caps
        if tool_name == "web_search":
            cap = caps.max_web_searches
            if cap and self._turn_web_search_count >= cap:
                decision = ToolGuardrailDecision(
                    action="block",
                    code="loop_web_search_cap",
                    message=(
                        f"Blocked web_search: this turn has already made {cap} "
                        "web searches, the per-turn limit. This looks like a "
                        "runaway search loop. Work with the results you already "
                        "have and give the user your answer."
                    ),
                    tool_name=tool_name,
                    count=self._turn_web_search_count,
                    signature=signature,
                )
                self._halt_decision = decision
                return decision
            self._turn_web_search_count += 1
            return None

        if tool_name == "delegate_task":
            cap = caps.max_subagents
            if not cap:
                return None
            spawn_count = _subagent_spawn_count(args)
            if self._turn_subagent_count >= cap:
                decision = ToolGuardrailDecision(
                    action="block",
                    code="loop_subagent_cap",
                    message=(
                        f"Blocked delegate_task: this turn has already spawned "
                        f"{self._turn_subagent_count} subagents (limit {cap}). "
                        "This looks like a runaway delegation loop. Finish the "
                        "work with the results you have and answer the user."
                    ),
                    tool_name=tool_name,
                    count=self._turn_subagent_count,
                    signature=signature,
                )
                self._halt_decision = decision
                return decision
            self._turn_subagent_count += spawn_count
        return None

    def _same_tool_halt_applies(self, tool_name: str) -> bool:
        return not (
            self.config.terminal_exact_failure_only
            and tool_name == "terminal"
        )


def toolguard_synthetic_result(decision: ToolGuardrailDecision) -> str:
    """Build a synthetic role=tool content string for a blocked tool call."""
    return json.dumps(
        {
            "error": decision.message,
            "guardrail": decision.to_metadata(),
        },
        ensure_ascii=False,
    )


def append_toolguard_guidance(result: str, decision: ToolGuardrailDecision) -> str:
    """Append runtime guidance to the current tool result content."""
    if decision.action not in {"warn", "halt"} or not decision.message:
        return result
    label = "Tool loop hard stop" if decision.action == "halt" else "Tool loop warning"
    suffix = (
        f"\n\n[{label}: "
        f"{decision.code}; count={decision.count}; {decision.message}]"
    )
    return (result or "") + suffix


def _tool_failure_recovery_hint(tool_name: str, count: int) -> str:
    """Action-oriented guidance for recovering from repeated tool failures."""
    common = (
        f"{tool_name} has failed {count} times this turn. This looks like a loop. "
        "Do not switch to text-only replies; keep using tools, but diagnose before retrying. "
        "First inspect the latest error/output and verify your assumptions. "
    )
    if tool_name == "terminal":
        return common + (
            "For terminal failures, run a small diagnostic such as `pwd && ls -la` "
            "in the same tool, then try an absolute path, a simpler command, a different "
            "working directory, or a different tool such as read_file/write_file/patch."
        )
    return common + (
        "Try different arguments, a narrower query/path, an absolute path when relevant, "
        "or a different tool that can make progress. If the blocker is external, report "
        "the blocker after one diagnostic attempt instead of repeating the same failing path."
    )


def _coerce_args(args: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return args if isinstance(args, Mapping) else {}


def _result_hash(result: str | None) -> str:
    parsed = safe_json_loads(result or "")
    if parsed is not None:
        try:
            canonical = json.dumps(
                parsed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except TypeError:
            canonical = str(parsed)
    else:
        canonical = result or ""
    return _sha256(canonical)


def _result_set_hash(results) -> str:
    """Hash a deduplicated result set independent of worker completion order."""
    members = sorted({_result_hash(result) for result in results})
    return _sha256(json.dumps(members, separators=(",", ":")))


def _typed_tool_error_policy(result: str | None) -> tuple[str, str]:
    """Extract a stable retry class/code from a typed tool error envelope."""
    parsed = safe_json_loads(result or "")
    if not isinstance(parsed, dict) and isinstance(result, str):
        # Guardrail guidance may be appended after a JSON result. Preserve the
        # envelope's typed policy by decoding only its first JSON value.
        try:
            parsed, _end = json.JSONDecoder().raw_decode(result.lstrip())
        except (json.JSONDecodeError, TypeError):
            parsed = None
    if not isinstance(parsed, dict):
        return "", ""
    error_class = str(parsed.get("error_class") or "").strip().lower()
    error_code = str(parsed.get("error_code") or "typed_tool_failure").strip()
    return error_class, error_code


def _typed_schema_retry_policy(result: str | None) -> dict[str, Any]:
    """Extract narrowly-scoped schema retry controls from a typed error."""
    parsed = safe_json_loads(result or "")
    if not isinstance(parsed, dict) and isinstance(result, str):
        try:
            parsed, _end = json.JSONDecoder().raw_decode(result.lstrip())
        except (json.JSONDecodeError, TypeError):
            parsed = None
    raw = parsed.get("retry_policy") if isinstance(parsed, dict) else None
    if not isinstance(raw, Mapping):
        raw = {}

    same_argument_retry_requested = (
        raw.get("requires_argument_change") is False
    )
    remediation_signature = None
    remediation = raw.get("remediation")
    if same_argument_retry_requested and isinstance(remediation, Mapping):
        remediation_tool = str(remediation.get("tool_name") or "").strip()
        remediation_args = remediation.get("arguments")
        if remediation_tool and isinstance(remediation_args, Mapping):
            remediation_signature = ToolCallSignature.from_call(
                remediation_tool,
                remediation_args,
            )
    # Same-argument retries are safe only behind a valid exact remediation.
    # Missing or malformed metadata falls back to changed arguments.
    requires_argument_change = not (
        same_argument_retry_requested and remediation_signature is not None
    )
    return {
        "requires_argument_change": requires_argument_change,
        "remediation_signature": remediation_signature,
        "remediation_satisfied": remediation_signature is None,
    }


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


def _positive_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


def _non_negative_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _subagent_spawn_count(args: Mapping[str, Any]) -> int:
    tasks = args.get("tasks") if isinstance(args, Mapping) else None
    if isinstance(tasks, list) and tasks:
        return len(tasks)
    return 1


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()
