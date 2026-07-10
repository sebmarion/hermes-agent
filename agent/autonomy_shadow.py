"""Fail-open, shadow-only turn-ingress planner.

This module may propose and validate an execution plan, but it cannot execute
one or alter the active turn route. Observations deliberately omit raw user
content and credentials.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.auxiliary_client import get_text_auxiliary_client
from agent.execution_plan import (
    EXECUTION_PLAN_JSON_SCHEMA,
    PlanValidationError,
    compile_execution_plan,
)
from hermes_cli.config import load_config
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_OBSERVATION_SCHEMA = "hermes.autonomy.shadow.v1"
_OBSERVATION_FILE = "shadow-observations.jsonl"
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="autonomy-shadow")
atexit.register(_executor.shutdown, wait=False, cancel_futures=True)
_write_lock = threading.Lock()
_recent_lock = threading.Lock()
_recent_turns: dict[str, float] = {}
_DEDUPE_TTL_SECONDS = 300.0


def _policy() -> dict[str, Any]:
    cfg = load_config() or {}
    raw = cfg.get("autonomy") or {}
    return raw if isinstance(raw, dict) else {}


def _is_enabled(policy: dict[str, Any]) -> bool:
    return policy.get("enabled") is True


def _prompt_text(message: Any) -> str:
    if isinstance(message, str):
        return message.strip()
    if isinstance(message, list):
        parts: list[str] = []
        for item in message:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n".join(parts).strip()
    return str(message or "").strip()


def _turn_key(prompt: str, session_id: str, source: str) -> str:
    payload = f"{session_id}\0{source}\0{prompt}".encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


def _claim_turn(key: str) -> bool:
    now = time.monotonic()
    with _recent_lock:
        stale = [item for item, seen in _recent_turns.items() if now - seen > _DEDUPE_TTL_SECONDS]
        for item in stale:
            _recent_turns.pop(item, None)
        if key in _recent_turns:
            return False
        _recent_turns[key] = now
        return True


def submit_shadow_observation(
    message: Any,
    *,
    session_id: str = "",
    source: str = "",
    workspace: str = "",
    parent_agent: Any = None,
) -> bool:
    """Schedule shadow planning without delaying or changing the active turn."""
    policy = _policy()
    if not _is_enabled(policy):
        return False
    prompt = _prompt_text(message)
    if not prompt:
        return False
    key = _turn_key(prompt, session_id, source)
    if not _claim_turn(key):
        return False
    try:
        _executor.submit(
            observe_turn,
            prompt,
            session_id=session_id,
            source=source,
            workspace=workspace,
            parent_agent=parent_agent,
        )
        return True
    except Exception as exc:
        with _recent_lock:
            _recent_turns.pop(key, None)
        logger.debug("autonomy shadow submission failed: %s", exc)
        return False


def _lane_for_capability(capability: str, config: dict[str, Any]) -> str | None:
    delegation = config.get("delegation") or {}
    if not isinstance(delegation, dict):
        delegation = {}
    lanes = delegation.get("lanes") or {}
    if not isinstance(lanes, dict):
        lanes = {}
    preferred = {
        "local_execution": "local_worker",
        "fast_fallback": "code_worker",
        "frontier_review": "smart_reviewer",
    }.get(capability, "")
    if preferred in lanes:
        return preferred
    default_lane = str(delegation.get("default_lane") or "").strip()
    if default_lane in lanes:
        return default_lane
    return None


def _plan_receipt(plan: Any, config: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": plan.mode,
        "risk": plan.risk,
        "slice_count": len(plan.slices),
        "wave_count": len(plan.dependency_waves),
        "capabilities": sorted({item.capability for item in plan.slices}),
        "lane_assignments": [
            {
                "slice_id": item.id,
                "capability": item.capability,
                "lane": _lane_for_capability(item.capability, config),
            }
            for item in plan.slices
        ],
        "escalation_predicate_count": len(plan.escalation_predicates),
    }


def observe_turn(
    message: Any,
    *,
    session_id: str = "",
    source: str = "",
    workspace: str = "",
    policy: dict[str, Any] | None = None,
    parent_agent: Any = None,
) -> dict[str, Any] | None:
    """Plan one turn in shadow mode and append a privacy-minimized receipt."""
    policy = _policy() if policy is None else policy
    if not _is_enabled(policy):
        return None
    config = load_config() or {}

    prompt = _prompt_text(message)
    configured_mode = str(policy.get("mode") or "shadow").strip().lower()
    effective_mode = configured_mode if configured_mode in {"shadow", "execute"} else "rejected"
    base: dict[str, Any] = {
        "schema": _OBSERVATION_SCHEMA,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "session_id": str(session_id or ""),
        "source": str(source or ""),
        "workspace_name": Path(str(workspace or "")).name,
        "workspace_sha256": hashlib.sha256(
            str(workspace or "").encode("utf-8", errors="replace")
        ).hexdigest(),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest(),
        "prompt_chars": len(prompt),
        "configured_mode": configured_mode,
        "effective_mode": effective_mode,
    }

    if configured_mode not in {"shadow", "execute"}:
        result = {
            **base,
            "status": "policy_rejected",
            "error": "Autonomy mode must be shadow or execute",
        }
        _append_observation(result)
        return result

    started = time.monotonic()
    try:
        timeout = max(1, int(policy.get("planner_timeout_seconds") or 30))
        raw = _plan_turn(prompt, workspace=workspace, timeout=timeout)
        plan = compile_execution_plan(raw)
        execution = None
        if configured_mode == "execute":
            from agent.autonomy_execution import dispatch_execution

            execution = dispatch_execution(
                plan,
                policy=None,
                config=config,
                parent_agent=parent_agent,
                session_id=session_id,
            )
        result = {
            **base,
            "status": "accepted",
            "duration_ms": round((time.monotonic() - started) * 1000),
            "plan": _plan_receipt(plan, config),
        }
        if execution is not None:
            result["execution"] = execution
    except PlanValidationError as exc:
        result = {
            **base,
            "status": "invalid_plan",
            "duration_ms": round((time.monotonic() - started) * 1000),
            "error": str(exc)[:500],
        }
    except Exception as exc:
        logger.debug("autonomy shadow planner failed: %s", exc)
        result = {
            **base,
            "status": "planner_error",
            "duration_ms": round((time.monotonic() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }
    _append_observation(result)
    return result


def _plan_turn(prompt: str, *, workspace: str, timeout: int) -> dict[str, Any]:
    client, model = get_text_auxiliary_client("execution_planner")
    if client is None or not model:
        raise RuntimeError("execution_planner auxiliary model is unavailable")
    client_name = type(client).__name__.lower()
    if "anthropic" in client_name or "gemini" in client_name:
        raise RuntimeError(
            "execution_planner requires a provider that preserves strict "
            "json_schema response_format; native Anthropic/Gemini adapters do not"
        )
    system = (
        "Propose a side-effect-free execution plan for the user request. "
        "Use capabilities, never provider/model names. Frontier review must only "
        "appear in explicit high-risk sota mode; local DAGs express escalation "
        "through escalation_predicates. Acceptance criteria must be observable."
    )
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": f"Workspace: {workspace or '.'}\nRequest:\n{prompt}"},
        ],
        temperature=0,
        max_tokens=3000,
        timeout=timeout,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "execution_plan",
                "strict": True,
                "schema": EXECUTION_PLAN_JSON_SCHEMA,
            },
        },
    )
    content = response.choices[0].message.content
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("execution_planner returned empty content")
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise RuntimeError("execution_planner did not return an object")
    return parsed


def _append_observation(row: dict[str, Any]) -> None:
    path = Path(get_hermes_home()) / "autonomy" / _OBSERVATION_FILE
    payload = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
    with _write_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
