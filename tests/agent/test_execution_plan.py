"""Deterministic contract tests for local-first execution plans."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.execution_plan import (
    EXECUTION_PLAN_GENERATION_SCHEMA,
    EXECUTION_PLAN_JSON_SCHEMA,
    PlanValidationError,
    compile_execution_plan,
    generate_execution_plan,
)


def _slice(slice_id: str, **overrides):
    value = {
        "id": slice_id,
        "kind": "implement",
        "goal": f"Complete {slice_id}",
        "depends_on": [],
        "capability": "local_execution",
        "workspace": "/repo",
        "allowed_paths": [f"src/{slice_id}.py"],
        "read_only": False,
        "expected_artifacts": [f"src/{slice_id}.py"],
        "acceptance": [f"pytest -q tests/test_{slice_id}.py"],
    }
    value.update(overrides)
    return value


def _plan(*slices, **overrides):
    value = {
        "version": 1,
        "mode": "delegate",
        "risk": "low",
        "slices": list(slices),
        "merge_policy": "Integrate dependency waves after verification.",
        "stop_condition": "All acceptance checks pass.",
        "escalation_predicates": ["verification_failed_after_local_repair"],
    }
    value.update(overrides)
    return value


def test_schema_is_strict_and_requires_observable_contract_fields():
    assert EXECUTION_PLAN_JSON_SCHEMA["additionalProperties"] is False
    assert set(EXECUTION_PLAN_JSON_SCHEMA["required"]) == {
        "version",
        "mode",
        "risk",
        "slices",
        "merge_policy",
        "stop_condition",
        "escalation_predicates",
    }
    slice_schema = EXECUTION_PLAN_JSON_SCHEMA["properties"]["slices"]["items"]
    assert slice_schema["additionalProperties"] is False
    assert "acceptance" in slice_schema["required"]
    assert "allowed_paths" in slice_schema["required"]


def _contains_key(value, target):
    if isinstance(value, dict):
        return target in value or any(
            _contains_key(item, target) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_key(item, target) for item in value)
    return False


def test_generation_schema_strips_only_local_grammar_length_bounds():
    assert _contains_key(EXECUTION_PLAN_JSON_SCHEMA, "minLength")
    assert _contains_key(EXECUTION_PLAN_JSON_SCHEMA, "maxLength")
    assert not _contains_key(EXECUTION_PLAN_GENERATION_SCHEMA, "minLength")
    assert not _contains_key(EXECUTION_PLAN_GENERATION_SCHEMA, "maxLength")
    assert EXECUTION_PLAN_GENERATION_SCHEMA["additionalProperties"] is False
    assert EXECUTION_PLAN_GENERATION_SCHEMA["properties"]["slices"]["maxItems"] == 6
    id_schema = EXECUTION_PLAN_JSON_SCHEMA["properties"]["slices"]["items"][
        "properties"
    ]["id"]
    assert id_schema["minLength"] == 1
    assert id_schema["maxLength"] == 64


def test_compile_valid_plan_orders_dependency_waves():
    raw = _plan(
        _slice("scout", kind="scout", read_only=True, allowed_paths=[]),
        _slice("implement", depends_on=["scout"]),
        _slice(
            "verify",
            kind="verify",
            read_only=True,
            allowed_paths=[],
            depends_on=["implement"],
            expected_artifacts=["test receipt"],
        ),
    )

    plan = compile_execution_plan(raw)

    assert plan.mode == "delegate"
    assert plan.dependency_waves == (("scout",), ("implement",), ("verify",))
    assert plan.slices[1].acceptance == ("pytest -q tests/test_implement.py",)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda plan: plan["slices"].append(_slice("build")), "duplicate slice id"),
        (
            lambda plan: plan["slices"][0].update(depends_on=["missing"]),
            "unknown dependency",
        ),
        (
            lambda plan: plan["slices"][0].update(depends_on=["build"]),
            "dependency cycle",
        ),
        (
            lambda plan: plan["slices"][0].update(acceptance=[]),
            "acceptance",
        ),
    ],
)
def test_compile_rejects_invalid_graphs_and_unverifiable_slices(mutate, match):
    raw = _plan(_slice("build"))
    mutate(raw)
    with pytest.raises(PlanValidationError, match=match):
        compile_execution_plan(raw)


def test_compile_rejects_parallel_overlapping_write_leases():
    raw = _plan(
        _slice("api", allowed_paths=["src/api"]),
        _slice("handler", allowed_paths=["src/api/handler.py"]),
    )

    with pytest.raises(PlanValidationError, match="overlapping write leases"):
        compile_execution_plan(raw)


def test_compile_allows_overlapping_paths_when_dependency_serializes_writes():
    raw = _plan(
        _slice("api", allowed_paths=["src/api"]),
        _slice(
            "handler",
            allowed_paths=["src/api/handler.py"],
            depends_on=["api"],
        ),
    )

    plan = compile_execution_plan(raw)
    assert plan.dependency_waves == (("api",), ("handler",))


def test_compile_rejects_unconditional_frontier_worker_slice():
    raw = _plan(_slice("judge", kind="review", capability="frontier_review"))

    with pytest.raises(PlanValidationError, match="frontier_review.*predicate"):
        compile_execution_plan(raw)


def test_explicit_sota_mode_is_one_high_risk_frontier_slice():
    raw = _plan(
        _slice("judge", kind="review", capability="frontier_review"),
        mode="sota",
        risk="high",
        escalation_predicates=["security_sensitive_request"],
    )

    plan = compile_execution_plan(raw)
    assert plan.mode == "sota"
    assert plan.slices[0].capability == "frontier_review"


def test_sota_mode_rejects_local_multi_slice_or_non_high_risk_graphs():
    invalid_plans = [
        _plan(
            _slice("local"),
            mode="sota",
            risk="high",
            escalation_predicates=["security_sensitive_request"],
        ),
        _plan(
            _slice("judge", kind="review", capability="frontier_review"),
            mode="sota",
            risk="medium",
            escalation_predicates=["security_sensitive_request"],
        ),
    ]
    for raw in invalid_plans:
        with pytest.raises(PlanValidationError, match="sota mode"):
            compile_execution_plan(raw)


def test_high_risk_plan_requires_frontier_escalation_predicate():
    raw = _plan(_slice("migration"), risk="high", escalation_predicates=[])

    with pytest.raises(PlanValidationError, match="high-risk"):
        compile_execution_plan(raw)


def test_direct_mode_must_have_exactly_one_slice():
    raw = _plan(_slice("one"), _slice("two"), mode="direct")

    with pytest.raises(PlanValidationError, match="direct mode"):
        compile_execution_plan(raw)


def test_generate_uses_strict_schema_and_repairs_one_semantic_failure():
    invalid = _plan(
        _slice("a", allowed_paths=["src/shared.py"]),
        _slice("b", allowed_paths=["src/shared.py"]),
    )
    valid = _plan(
        _slice("a", allowed_paths=["src/shared.py"]),
        _slice("b", allowed_paths=["src/shared.py"], depends_on=["a"]),
    )
    responses = iter([invalid, valid])
    calls = []

    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            payload = next(responses)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))

    plan = generate_execution_plan(
        "Implement safely",
        client=client,
        model="local-planner",
        max_repair_attempts=1,
    )

    assert plan.dependency_waves == (("a",), ("b",))
    assert len(calls) == 2
    response_format = calls[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"] is EXECUTION_PLAN_GENERATION_SCHEMA
    assert "Validation failed" in calls[1]["messages"][-1]["content"]


def test_generate_stops_after_bounded_repair_attempt():
    invalid = _plan(_slice("bad", acceptance=[]))

    class Completions:
        def create(self, **_kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(invalid)))]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))

    with pytest.raises(PlanValidationError, match="acceptance"):
        generate_execution_plan(
            "Implement safely",
            client=client,
            model="local-planner",
            max_repair_attempts=1,
        )
