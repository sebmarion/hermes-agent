"""Tests for agent.bestplan_orchestrator.

These assert *invariants* (lane structure, safety constraints, receipt
integrity) rather than snapshot literal model strings.  Model strings are
config-owned and change when SOTA models are updated; the contracts below
must hold regardless of which model names are configured.
"""

from pathlib import Path

from agent.bestplan_orchestrator import (
    BestPlanUnavailable, DEFAULT_RUNTIME, RECEIPT_BEGIN, RECEIPT_END, append_receipt,
    body_sha256, build_explorer_schedule, make_receipt, normalize_count, quorum_for,
    reconcile_bestplan_receipts, run_bestplan, validate_receipt, validate_runtime,
    _resolve_lane_credentials, _run_child_with_timeout,
)

_REQUIRED_LANE_KEYS = ("name", "provider", "model", "api_mode", "reasoning_effort")


def test_count_and_quorum():
    assert normalize_count(1) == 2
    assert normalize_count(9) == 5
    assert [quorum_for(n) for n in range(2, 6)] == [2, 2, 3, 4]


def _default_lanes_by_name() -> dict:
    return {lane["name"]: lane for lane in DEFAULT_RUNTIME["lanes"]}


def test_default_runtime_has_two_validated_lanes():
    """DEFAULT_RUNTIME provides exactly two lanes named 'glm' and 'sol',
    each with all required keys."""
    lanes = DEFAULT_RUNTIME["lanes"]
    assert len(lanes) == 2
    names = {lane["name"] for lane in lanes}
    assert names == {"glm", "sol"}
    for lane in lanes:
        for key in _REQUIRED_LANE_KEYS:
            assert lane.get(key), f"lane '{lane.get('name')}' missing key '{key}'"


def test_validate_runtime_accepts_default_config():
    """validate_runtime() with no config must succeed (uses DEFAULT_RUNTIME)."""
    cfg = validate_runtime()
    assert len(cfg["lanes"]) == 2
    assert {lane["name"] for lane in cfg["lanes"]} == {"glm", "sol"}


def test_validate_runtime_accepts_config_lanes_with_arbitrary_models():
    """When config supplies lanes with different model strings, validate_runtime
    must accept them as long as the structure invariant holds."""
    custom_lanes = [
        {"name": "glm", "provider": "custom:neuralwatt", "model": "glm-5.3-fast",
         "api_mode": "chat_completions", "reasoning_effort": "high"},
        {"name": "sol", "provider": "openai-codex", "model": "gpt-6-sol",
         "api_mode": "codex_app_server", "reasoning_effort": "ultra"},
    ]
    cfg = validate_runtime({"lanes": custom_lanes})
    assert cfg["lanes"][0]["model"] == "glm-5.3-fast"
    assert cfg["lanes"][1]["model"] == "gpt-6-sol"


def test_validate_runtime_accepts_mapping_lanes():
    """YAML mappings such as {glm: {...}, sol: {...}} normalize to named lanes."""
    cfg = validate_runtime({
        "lanes": {
            "primary": {
                "provider": "provider-a",
                "model": "model-a",
                "api_mode": "chat_completions",
                "reasoning_effort": "high",
            },
            "secondary": {
                "provider": "provider-b",
                "model": "model-b",
                "api_mode": "chat_completions",
                "reasoning_effort": "medium",
            },
        }
    })
    assert [lane["name"] for lane in cfg["lanes"]] == ["primary", "secondary"]


def test_lane_credentials_forward_explicit_overrides(monkeypatch):
    captured = {}

    def fake_resolver(**kwargs):
        captured.update(kwargs)
        return {
            "provider": "custom-provider",
            "api_mode": "chat_completions",
            "base_url": kwargs["explicit_base_url"],
            "api_key": kwargs["explicit_api_key"],
        }

    import hermes_cli.runtime_provider
    monkeypatch.setattr(hermes_cli.runtime_provider, "resolve_runtime_provider", fake_resolver)
    lane = {
        "name": "custom",
        "provider": "custom-provider",
        "model": "custom-model",
        "api_mode": "chat_completions",
        "reasoning_effort": "high",
        "api_key": "lane-key",
        "base_url": "https://lane.example/v1",
    }
    runtime = _resolve_lane_credentials(object(), lane)
    assert runtime["api_key"] == "lane-key"
    assert runtime["base_url"] == "https://lane.example/v1"
    assert captured["explicit_api_key"] == "lane-key"
    assert captured["explicit_base_url"] == "https://lane.example/v1"


def test_codex_does_not_use_foreign_parent_credentials(monkeypatch, tmp_path):
    import hermes_cli.runtime_provider
    monkeypatch.setattr(
        hermes_cli.runtime_provider,
        "resolve_runtime_provider",
        lambda **kwargs: {"provider": "openai-codex", "api_mode": "codex_responses", "api_key": ""},
    )
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    lane = {
        "name": "codex",
        "provider": "openai-codex",
        "model": "gpt-test",
        "api_mode": "codex_app_server",
        "reasoning_effort": "ultra",
    }
    parent = type("Parent", (), {"provider": "zai", "api_key": "foreign-key", "_credential_pool": object()})()
    try:
        _resolve_lane_credentials(parent, lane)
    except BestPlanUnavailable as exc:
        assert "Codex credentials" in str(exc)
    else:
        raise AssertionError("foreign parent credentials activated Codex")


def test_lane_resolution_non_bestplan_exception_isolated(monkeypatch):
    import agent.bestplan_orchestrator as orchestrator
    lanes = [
        {"name": "bad", "provider": "bad", "model": "bad", "api_mode": "chat_completions", "reasoning_effort": "high"},
        {"name": "good", "provider": "good", "model": "good", "api_mode": "chat_completions", "reasoning_effort": "high"},
    ]

    def fake_resolver(_agent, lane):
        if lane["name"] == "bad":
            raise TypeError("malformed provider response")
        return {"provider": "good", "requested_provider": "good", "model": "good", "api_mode": "chat_completions"}

    monkeypatch.setattr(orchestrator, "_resolve_lane_credentials", fake_resolver)
    active, unavailable = orchestrator._active_lane_records(object(), lanes)
    assert [record["lane"]["name"] for record in active] == ["good"]
    assert unavailable == ["bad: TypeError"]


def test_child_timeout_is_bounded():
    import time

    def hanging_child(_prompt, _record):
        time.sleep(0.2)
        return "late"

    started = time.monotonic()
    try:
        _run_child_with_timeout(hanging_child, "prompt", {}, 0.01)
    except TimeoutError:
        pass
    else:
        raise AssertionError("hanging child was not timed out")
    assert time.monotonic() - started < 0.1


def test_sol_ultra_requires_codex_app_server():
    """If a lane has reasoning_effort='ultra' but api_mode != 'codex_app_server',
    validate_runtime must raise BestPlanUnavailable — the ultra→codex_app_server
    safety contract (see codex_responses_adapter.py:50-55)."""
    bad_lanes = [
        {"name": "glm", "provider": "custom:neuralwatt", "model": "glm-5.2",
         "api_mode": "chat_completions", "reasoning_effort": "high"},
        {"name": "sol", "provider": "openai-codex", "model": "gpt-5.6-sol",
         "api_mode": "codex_responses", "reasoning_effort": "ultra"},
    ]
    try:
        validate_runtime({"lanes": bad_lanes})
    except BestPlanUnavailable as exc:
        assert "codex_app_server" in str(exc)
        return
    raise AssertionError("validate_runtime accepted ultra without codex_app_server")


def test_empty_lane_count_rejected():
    """An empty lane list must raise BestPlanUnavailable."""
    empty_lanes = []
    try:
        validate_runtime({"lanes": empty_lanes})
    except BestPlanUnavailable:
        pass
    else:
        raise AssertionError("empty lanes were accepted")


def test_single_lane_is_valid():
    """A single configured lane is valid; runtime availability is separate."""
    lane = [{"name": "top", "provider": "p", "model": "m", "api_mode": "c", "reasoning_effort": "h"}]
    assert validate_runtime({"lanes": lane})["lanes"] == lane


def test_missing_required_lane_key_rejected():
    """A lane missing a required key must raise BestPlanUnavailable."""
    bad_lanes = [
        {"name": "glm", "provider": "p", "model": "m", "api_mode": "c"},  # missing reasoning_effort
        {"name": "sol", "provider": "p", "model": "m", "api_mode": "c", "reasoning_effort": "h"},
    ]
    try:
        validate_runtime({"lanes": bad_lanes})
    except BestPlanUnavailable as exc:
        assert "reasoning_effort" in str(exc) or "missing" in str(exc)
        return
    raise AssertionError("lane missing a required key was accepted")


def test_lane_names_are_config_owned():
    """BestPlan accepts arbitrary lane names; provider resolution owns activity."""
    lanes = [
        {"name": "fast", "provider": "p", "model": "m", "api_mode": "c", "reasoning_effort": "h"},
        {"name": "slow", "provider": "p", "model": "m", "api_mode": "c", "reasoning_effort": "h"},
    ]
    assert validate_runtime({"lanes": lanes})["lanes"] == lanes


def _record(name, provider, model, index, priority):
    return {
        "lane": {"name": name, "provider": provider, "model": model},
        "credentials": {"provider": provider, "requested_provider": provider, "model": model, "api_mode": "chat_completions"},
        "index": index,
        "priority": priority,
    }


def test_single_provider_uses_three_top_model_replicas():
    records = [
        _record("small", "provider-a", "model-small", 0, 1),
        _record("top", "provider-a", "model-top", 1, 2),
    ]
    schedule, mode = build_explorer_schedule(records, count=5)
    assert mode == "single_provider_moe"
    assert len(schedule) == 3
    assert {item["lane"]["model"] for item in schedule} == {"model-top"}


def test_multiple_providers_keep_requested_fanout():
    records = [
        _record("a", "provider-a", "model-a", 0, 1),
        _record("b", "provider-b", "model-b", 1, 2),
    ]
    schedule, mode = build_explorer_schedule(records, count=4)
    assert mode == "heterogeneous"
    assert len(schedule) == 4
    assert [item["lane"]["model"] for item in schedule] == ["model-a", "model-b", "model-a", "model-b"]


def test_run_bestplan_single_provider_uses_three_top_model_instances(monkeypatch, tmp_path):
    """Live orchestration keeps one-provider MoE resilient below quorum."""
    import agent.bestplan_orchestrator as orchestrator
    import run_agent

    calls = []

    class FakeAgent:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def run_conversation(self, prompt):
            if "Return exactly one JSON object" not in prompt:
                return {"final_response": "synthesized plan"}
            if "evidence-first" in prompt or "counterfactual" in prompt:
                return {"final_response": "malformed candidate"}
            return {
                "final_response": (
                    "HERMES_BESTPLAN_CANDIDATE_V1 "
                    '{"schema":"HERMES_BESTPLAN_CANDIDATE_V1","summary":"s","steps":["step"],'
                    '"risks":["risk"],"verification":["check"]}'
                )
            }

        def close(self):
            pass

    def fake_resolver(_agent, lane):
        return {
            "provider": lane["provider"],
            "requested_provider": lane["provider"],
            "model": lane["model"],
            "api_mode": "chat_completions",
        }

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    monkeypatch.setattr(orchestrator, "_resolve_lane_credentials", fake_resolver)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    outcome = run_bestplan(
        object(),
        "plan this test",
        count=5,
        config={
            "lanes": [
                {"name": "small", "provider": "provider-a", "model": "small", "api_mode": "chat_completions", "reasoning_effort": "high", "priority": 1},
                {"name": "top", "provider": "provider-a", "model": "top", "api_mode": "chat_completions", "reasoning_effort": "high", "priority": 2},
            ]
        },
    )

    assert outcome["status"] == "completed"
    assert outcome["provider_mode"] == "single_provider_moe"
    assert outcome["active_providers"] == 1
    assert outcome["successes"] == 1
    assert outcome["degraded"] is True
    assert len(calls) == 4  # three explorers + one synthesizer
    assert {call["model"] for call in calls} == {"top"}


def test_receipt_has_canonical_markers_and_hash():
    body = "plan body"
    receipt = make_receipt("run-1", model="gpt-5.6-sol", quorum="3/3", synth_status="success", body=body, lane="sol")
    assert receipt.startswith(RECEIPT_BEGIN)
    assert receipt.endswith(RECEIPT_END)
    assert validate_receipt(receipt, body)
    assert not validate_receipt(receipt, body + "!")
    assert body_sha256(body)


def test_append_and_reconcile_is_idempotent(tmp_path):
    path = tmp_path / "receipts.jsonl"
    append_receipt(path, {"run_id": "run-1", "status": "running"})
    assert reconcile_bestplan_receipts(path) == ["run-1"]
    assert reconcile_bestplan_receipts(path) == []
