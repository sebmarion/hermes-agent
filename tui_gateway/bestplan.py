from __future__ import annotations

from typing import Any


def capture_gateway_bestplan_result(
    result: dict,
    *,
    invocation_message: str,
    session_id: str,
    profile: str,
    workspace: str,
    host_agent: Any,
    baseline_fingerprint: str | None = None,
    store: Any = None,
) -> dict:
    """Capture a gateway BestPlan only after its receipt is durable."""
    from agent.bestplan_state import (
        BestplanStore,
        capture_bestplan_agent_result,
    )

    owns_store = store is None
    if owns_store:
        store = BestplanStore()

    try:
        captured = capture_bestplan_agent_result(
            result,
            invocation_message=invocation_message,
            session_id=session_id,
            profile=profile,
            workspace=workspace,
            baseline_fingerprint=baseline_fingerprint,
            store=store,
            host_agent=host_agent,
            provisional=True,
            local_execution=True,
        )

        bestplan_capture = (
            captured.get("bestplan_capture")
            if isinstance(captured, dict)
            else None
        )
        if not isinstance(bestplan_capture, dict):
            return captured

        if bestplan_capture.get("executable") is True:
            plan_id = str(bestplan_capture.get("plan_id") or "").strip()
            if not callable(getattr(host_agent, "_persist_session", None)):
                raise RuntimeError(
                    "Gateway BestPlan receipt persistence unavailable"
                )
            if bestplan_capture.get("receipt_persisted") is not True:
                raise RuntimeError("Gateway BestPlan receipt persistence failed")
            if not plan_id or not store.commit_provisional_plan(plan_id):
                raise RuntimeError(
                    "Gateway BestPlan provisional capture could not be committed"
                )

        return captured
    finally:
        if owns_store:
            store.close()
