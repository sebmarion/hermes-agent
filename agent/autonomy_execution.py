"""Strict promotion gate for bounded autonomous read-only execution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from agent.execution_plan import ExecutionPlan
from hermes_cli.config import load_config

_READ_ONLY_TOOLS = frozenset({"read_file", "search_files", "web_search", "web_extract"})
_MAX_EXECUTION_SLICES = 2


@dataclass(frozen=True)
class ExecutionDecision:
    eligible: bool
    reason: str
    lane: str | None = None
    slice_ids: tuple[str, ...] = ()


def _normalized_tools(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def evaluate_execution(
    plan: ExecutionPlan,
    *,
    policy: dict[str, Any],
    config: dict[str, Any],
) -> ExecutionDecision:
    """Return a deterministic decision without performing side effects."""
    if not (
        policy.get("enabled") is True
        and str(policy.get("mode") or "").strip().lower() == "execute"
        and policy.get("execution_enabled") is True
    ):
        return ExecutionDecision(False, "controlled execution is disabled")
    if plan.risk != "low":
        return ExecutionDecision(False, "only low-risk plans may execute")
    if plan.mode not in {"direct", "delegate"}:
        return ExecutionDecision(False, "only direct or delegate plans may execute")
    if len(plan.slices) > _MAX_EXECUTION_SLICES:
        return ExecutionDecision(False, f"execution is limited to {_MAX_EXECUTION_SLICES} slices")
    if len(plan.dependency_waves) != 1:
        return ExecutionDecision(False, "only a single dependency wave may execute")
    if any(not item.read_only for item in plan.slices):
        return ExecutionDecision(False, "all execution slices must be read-only")
    if any(item.capability != "local_execution" for item in plan.slices):
        return ExecutionDecision(False, "all execution slices must use local_execution")

    delegation = config.get("delegation") or {}
    lanes = delegation.get("lanes") or {} if isinstance(delegation, dict) else {}
    lane = lanes.get("local_worker") if isinstance(lanes, dict) else None
    if not isinstance(lane, dict):
        return ExecutionDecision(False, "local_worker lane is not configured")
    tools = _normalized_tools(lane.get("toolsets"))
    if not tools:
        return ExecutionDecision(False, "local_worker requires an explicit read-only tool allowlist")
    unsafe = sorted(set(tools) - _READ_ONLY_TOOLS)
    if unsafe:
        return ExecutionDecision(
            False,
            "local_worker tool allowlist is not read-only: " + ", ".join(unsafe),
        )
    if not str(lane.get("provider") or "").strip() or not str(lane.get("model") or "").strip():
        return ExecutionDecision(False, "local_worker provider and model must be explicit")
    return ExecutionDecision(
        True,
        "eligible",
        lane="local_worker",
        slice_ids=tuple(item.id for item in plan.slices),
    )


def _tasks(plan: ExecutionPlan, lane: str) -> list[dict[str, Any]]:
    result = []
    for item in plan.slices:
        context = (
            "Controlled autonomy canary. Inspect only; do not modify files, run shell "
            "commands, create resources, message users, or perform external writes.\n"
            f"Workspace: {item.workspace or '.'}\n"
            f"Allowed paths: {', '.join(item.allowed_paths) or '(none)'}\n"
            f"Expected artifacts: {'; '.join(item.expected_artifacts)}\n"
            f"Acceptance: {'; '.join(item.acceptance)}"
        )
        result.append({
            "goal": item.goal,
            "context": context,
            "role": "leaf",
            "route": lane,
        })
    return result


def _delegation_ids(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        candidate = value.get("delegation_id")
        if isinstance(candidate, str) and candidate.startswith("deleg_"):
            found.append(candidate)
        candidates = value.get("delegation_ids")
        if isinstance(candidates, list):
            found.extend(
                item for item in candidates
                if isinstance(item, str) and item.startswith("deleg_")
            )
        for key in ("results", "dispatches"):
            child = value.get(key)
            if isinstance(child, list):
                for item in child:
                    found.extend(_delegation_ids(item))
    return list(dict.fromkeys(found))


def dispatch_execution(
    plan: ExecutionPlan,
    *,
    policy: dict[str, Any] | None,
    config: dict[str, Any],
    parent_agent: Any,
    delegate: Callable[..., Any] | None = None,
    session_id: str = "",
) -> dict[str, Any]:
    """Recheck live policy and dispatch eligible slices as background children."""
    if policy is None:
        live_config = load_config() or {}
        raw_policy = live_config.get("autonomy") or {}
        effective_policy = raw_policy if isinstance(raw_policy, dict) else {}
        config = live_config
    else:
        effective_policy = policy
    decision = evaluate_execution(plan, policy=effective_policy, config=config)
    if not decision.eligible:
        return {"status": "rejected", "reason": decision.reason}
    if parent_agent is None:
        return {"status": "rejected", "reason": "a live parent agent is required"}
    if delegate is None:
        from tools.delegate_tool import delegate_task

        delegate = delegate_task
    raw = delegate(
        tasks=_tasks(plan, decision.lane or "local_worker"),
        role="leaf",
        background=True,
        parent_agent=parent_agent,
    )
    try:
        decoded = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        decoded = {"status": "error", "error": "delegate returned malformed JSON"}
    status = decoded.get("status") if isinstance(decoded, dict) else None
    if status not in {"dispatched", "completed"} and isinstance(decoded, dict):
        # delegate_task returns a results envelope for some async batch paths.
        status = "dispatched" if "results" in decoded else status
    return {
        "status": status or "error",
        "lane": decision.lane,
        "slice_count": len(plan.slices),
        "slice_ids": list(decision.slice_ids),
        "session_id": str(session_id or ""),
        "delegation_ids": _delegation_ids(decoded),
    }
