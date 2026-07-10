"""Controlled autonomy execution promotion tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent import autonomy_execution as execution
from agent.execution_plan import compile_execution_plan


def _plan(*, risk="low", read_only=True, capability="local_execution", slices=1):
    raw = {
        "version": 1,
        "mode": "direct" if slices == 1 else "delegate",
        "risk": risk,
        "slices": [],
        "merge_policy": "independent read-only results",
        "stop_condition": "all acceptance checks reported",
        "escalation_predicates": [],
    }
    for index in range(slices):
        raw["slices"].append({
            "id": f"slice-{index}",
            "kind": "scout",
            "goal": f"Inspect subsystem {index}",
            "depends_on": [],
            "capability": capability,
            "workspace": ".",
            "allowed_paths": [f"src/{index}"],
            "read_only": read_only,
            "expected_artifacts": ["findings"],
            "acceptance": ["cite inspected files"],
        })
    return compile_execution_plan(raw)


def _config(*, tools=None):
    return {
        "delegation": {
            "lanes": {
                "local_worker": {
                    "provider": "custom:zeus",
                    "model": "glm-4.7-flash",
                    "toolsets": ["read_file", "search_files"] if tools is None else tools,
                }
            }
        }
    }


def test_execution_is_disabled_without_both_gates():
    plan = _plan()
    for policy in (
        {},
        {"enabled": True, "mode": "execute"},
        {"enabled": True, "mode": "execute", "execution_enabled": False},
    ):
        decision = execution.evaluate_execution(plan, policy=policy, config=_config())
        assert decision.eligible is False
        assert "disabled" in decision.reason


def test_only_low_risk_read_only_local_single_wave_plans_are_eligible():
    policy = {"enabled": True, "mode": "execute", "execution_enabled": True}

    decision = execution.evaluate_execution(_plan(slices=2), policy=policy, config=_config())

    assert decision.eligible is True
    assert decision.lane == "local_worker"
    assert decision.slice_ids == ("slice-0", "slice-1")


@pytest.mark.parametrize(
    "plan, reason",
    [
        (_plan(risk="medium"), "low-risk"),
        (_plan(read_only=False), "read-only"),
        (_plan(capability="fast_fallback"), "local_execution"),
    ],
)
def test_unsafe_plans_are_rejected(plan, reason):
    policy = {"enabled": True, "mode": "execute", "execution_enabled": True}

    decision = execution.evaluate_execution(plan, policy=policy, config=_config())

    assert decision.eligible is False
    assert reason in decision.reason


def test_lane_must_have_explicit_read_only_tool_allowlist():
    policy = {"enabled": True, "mode": "execute", "execution_enabled": True}

    missing = execution.evaluate_execution(_plan(), policy=policy, config=_config(tools=[]))
    unsafe = execution.evaluate_execution(
        _plan(), policy=policy, config=_config(tools=["read_file", "terminal"])
    )

    assert missing.eligible is False
    assert "explicit" in missing.reason
    assert unsafe.eligible is False
    assert "read-only" in unsafe.reason


def test_dispatch_builds_bounded_local_leaf_tasks_and_receipt():
    captured = {}

    def delegate(**kwargs):
        captured.update(kwargs)
        return json.dumps({"status": "dispatched", "delegation_ids": ["deleg_1"]})

    receipt = execution.dispatch_execution(
        _plan(slices=2),
        policy={"enabled": True, "mode": "execute", "execution_enabled": True},
        config=_config(),
        parent_agent=SimpleNamespace(),
        delegate=delegate,
        session_id="session-1",
    )

    assert receipt["status"] == "dispatched"
    assert receipt["lane"] == "local_worker"
    assert receipt["slice_count"] == 2
    assert receipt["delegation_ids"] == ["deleg_1"]
    assert "delegate" not in receipt
    assert "Inspect subsystem" not in json.dumps(receipt)
    assert captured["background"] is True
    assert captured["parent_agent"] is not None
    assert [task["route"] for task in captured["tasks"]] == ["local_worker", "local_worker"]
    assert all(task["role"] == "leaf" for task in captured["tasks"])
    assert "Inspect only" in captured["tasks"][0]["context"]


def test_dispatch_rechecks_live_policy_before_side_effect(monkeypatch):
    calls = []
    disabled = _config()
    disabled["autonomy"] = {
        "enabled": False,
        "mode": "execute",
        "execution_enabled": True,
    }
    monkeypatch.setattr(execution, "load_config", lambda: disabled)

    receipt = execution.dispatch_execution(
        _plan(),
        policy=None,
        config=_config(),
        parent_agent=SimpleNamespace(),
        delegate=lambda **kwargs: calls.append(kwargs),
    )

    assert receipt["status"] == "rejected"
    assert calls == []


def test_dispatch_requires_parent_agent():
    receipt = execution.dispatch_execution(
        _plan(),
        policy={"enabled": True, "mode": "execute", "execution_enabled": True},
        config=_config(),
        parent_agent=None,
    )
    assert receipt["status"] == "rejected"
    assert "parent agent" in receipt["reason"]
