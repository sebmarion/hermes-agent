"""Tests for agent.bestplan_orchestrator.

These assert *invariants* (lane structure, safety constraints, receipt
integrity) rather than snapshot literal model strings.  Model strings are
config-owned and change when SOTA models are updated; the contracts below
must hold regardless of which model names are configured.
"""

from agent.bestplan_orchestrator import (
    BestPlanUnavailable, DEFAULT_RUNTIME, RECEIPT_BEGIN, RECEIPT_END, append_receipt,
    body_sha256, make_receipt, normalize_count, quorum_for, reconcile_bestplan_receipts,
    validate_receipt, validate_runtime,
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


def test_invalid_lane_count_rejected():
    """One lane or three lanes must raise BestPlanUnavailable."""
    one_lane = [{"name": "glm", "provider": "p", "model": "m", "api_mode": "c", "reasoning_effort": "h"}]
    try:
        validate_runtime({"lanes": one_lane})
    except BestPlanUnavailable:
        pass
    else:
        raise AssertionError("one lane was accepted")

    three_lanes = [
        {"name": "glm", "provider": "p", "model": "m", "api_mode": "c", "reasoning_effort": "h"},
        {"name": "sol", "provider": "p", "model": "m", "api_mode": "c", "reasoning_effort": "h"},
        {"name": "extra", "provider": "p", "model": "m", "api_mode": "c", "reasoning_effort": "h"},
    ]
    try:
        validate_runtime({"lanes": three_lanes})
    except BestPlanUnavailable:
        pass
    else:
        raise AssertionError("three lanes were accepted")


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


def test_wrong_lane_names_rejected():
    """Lanes not named 'glm' and 'sol' must raise BestPlanUnavailable."""
    wrong_names = [
        {"name": "fast", "provider": "p", "model": "m", "api_mode": "c", "reasoning_effort": "h"},
        {"name": "slow", "provider": "p", "model": "m", "api_mode": "c", "reasoning_effort": "h"},
    ]
    try:
        validate_runtime({"lanes": wrong_names})
    except BestPlanUnavailable:
        pass
    else:
        raise AssertionError("wrong lane names were accepted")


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
