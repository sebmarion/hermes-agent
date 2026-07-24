"""Tests for agent.bestplan_orchestrator.

These assert *invariants* (lane structure, safety constraints, receipt
integrity) rather than snapshot literal model strings.  Model strings are
config-owned and change when SOTA models are updated; the contracts below
must hold regardless of which model names are configured.
"""

import json
import pytest
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from agent.bestplan_orchestrator import (
    BestPlanUnavailable, DEFAULT_RUNTIME, RECEIPT_BEGIN, RECEIPT_END, append_receipt,
    body_sha256, make_receipt, normalize_count, quorum_for, reconcile_bestplan_receipts,
    run_bestplan, validate_receipt, validate_runtime,
)

_REQUIRED_LANE_KEYS = ("name", "provider", "model", "api_mode", "reasoning_effort")


def _candidate_text(label="ok"):
    return "HERMES_BESTPLAN_CANDIDATE_V1\n" + json.dumps(
        {
            "schema": "HERMES_BESTPLAN_CANDIDATE_V1",
            "summary": label,
            "steps": ["step"],
            "risks": ["risk"],
            "verification": ["verify"],
        }
    )


def _latest_receipt_record(home: Path) -> dict:
    return json.loads(
        (home / "bestplan" / "receipts.jsonl").read_text().splitlines()[-1]
    )


def _runtime_config(**overrides):
    config = {
        "lanes": [
            {
                "name": "glm",
                "provider": "configured-glm",
                "model": "configured-glm-model",
                "api_mode": "chat_completions",
                "reasoning_effort": "high",
            },
            {
                "name": "sol",
                "provider": "openai-codex",
                "model": "configured-sol-model",
                "api_mode": "codex_app_server",
                "reasoning_effort": "ultra",
            },
        ],
        "explorer_timeout": 180,
        "synthesizer_timeout": 180,
        "overall_timeout": 540,
    }
    config.update(overrides)
    return config

def _canonical_config(**overrides):
    config = {
        "explorers": [
            {
                "name": "glm",
                "provider": "configured-glm",
                "model": "configured-glm-model",
                "api_mode": "chat_completions",
                "reasoning_effort": "high",
            },
            {
                "name": "kimi-k3",
                "provider": "configured-kimi",
                "model": "configured-kimi-model",
                "api_mode": "anthropic_messages",
                "reasoning_effort": "max",
            },
            {
                "name": "sol",
                "provider": "openai-codex",
                "model": "configured-sol-model",
                "api_mode": "codex_app_server",
                "reasoning_effort": "ultra",
            },
        ],
        "synthesizer": "sol",
        "explorer_timeout": 180,
        "synthesizer_timeout": 180,
        "overall_timeout": 540,
    }
    config.update(overrides)
    return config


def _assert_valid_explorer(entry: dict) -> None:
    required = ("name", "provider", "model", "api_mode", "reasoning_effort")
    assert set(entry.keys()) == set(required)
    assert all(isinstance(entry.get(k), str) for k in required)


def _canonical_explorer(**overrides):
    entry = {
        "name": "configured-name",
        "provider": "configured-provider",
        "model": "configured-model",
        "api_mode": "chat_completions",
        "reasoning_effort": "high",
    }
    entry.update(overrides)
    return entry


def _allow_subsecond_runtime_for_timing_tests(monkeypatch, orchestrator):
    """Validate structure normally while preserving tiny runtime deadlines."""
    original_validate = orchestrator.validate_runtime

    def validate_for_test(config=None, *, credentials_available=True):
        requested = dict(config or {})
        safe = dict(requested)
        timeout_keys = (
            "explorer_timeout", "synthesizer_timeout", "overall_timeout",
        )
        for key in timeout_keys:
            if key in safe:
                safe[key] = max(1.0, safe[key])
        resolved = original_validate(
            safe if config is not None else None,
            credentials_available=credentials_available,
        )
        for key in timeout_keys:
            if key in requested:
                resolved[key] = requested[key]
        return resolved

    monkeypatch.setattr(orchestrator, "validate_runtime", validate_for_test)


# ---------------------------------------------------------------------------
# Canonical schema tests
# ---------------------------------------------------------------------------

def test_canonical_config_requires_explorers_and_synthesizer():
    """Without explorers or synthesizer in canonical form, validation must fail."""
    try:
        validate_runtime(_canonical_config(explorers=None))
    except BestPlanUnavailable:
        pass
    else:
        raise AssertionError("canonical config missing explorers was accepted")
    try:
        validate_runtime(_canonical_config(synthesizer=None))
    except BestPlanUnavailable:
        pass
    else:
        raise AssertionError("canonical config missing synthesizer was accepted")


def test_one_through_five_explorers_validate():
    """Between one and five distinct named explorers must validate."""
    for size in range(1, 6):
        explorers = [
            _canonical_explorer(name=f"explorer-{i}")
            for i in range(size)
        ]
        validate_runtime(_canonical_config(explorers=explorers, synthesizer="explorer-0"))


def test_six_explorers_rejected():
    """Six explorers must fail validation."""
    explorers = [
        _canonical_explorer(name=f"explorer-{i}")
        for i in range(6)
    ]
    try:
        validate_runtime(_canonical_config(explorers=explorers, synthesizer="explorer-0"))
    except BestPlanUnavailable:
        pass
    else:
        raise AssertionError("six explorers was accepted")


def test_duplicate_normalized_names_rejected():
    """Names are case-insensitive after normalization; duplicates must fail."""
    explorers = [
        _canonical_explorer(name="foo"),
        _canonical_explorer(name="FOO"),
        _canonical_explorer(name="  Foo  "),
    ]
    try:
        validate_runtime(_canonical_config(explorers=explorers, synthesizer="foo"))
    except BestPlanUnavailable:
        pass
    else:
        raise AssertionError("duplicate explorer names were accepted")


def test_unknown_explorer_keys_rejected():
    """Extra keys on explorer entries must fail validation."""
    explorers = [
        _canonical_explorer(**{"name": "glm", "provider": "p", "model": "m", "api_mode": "c", "reasoning_effort": "h", "bogus": "x"}),
        _canonical_explorer(name="sol", provider="p", model="m", api_mode="codex_app_server", reasoning_effort="ultra"),
    ]
    try:
        validate_runtime(_canonical_config(explorers=explorers, synthesizer="glm"))
    except BestPlanUnavailable:
        pass
    else:
        raise AssertionError("unknown explorer key was accepted")


def test_empty_string_explorer_field_rejected():
    """Every non-empty string explorer field must reject empty strings after trim."""
    explorers = [
        _canonical_explorer(name=""),
        _canonical_explorer(provider=""),
        _canonical_explorer(model=""),
        _canonical_explorer(api_mode=""),
        _canonical_explorer(reasoning_effort=""),
    ]
    for entry in explorers:
        with pytest.raises(BestPlanUnavailable):
            validate_runtime(_canonical_config(explorers=[entry], synthesizer="test"))


def test_invalid_api_modes_rejected():
    """api_mode must be one of the allowed enum values."""
    for api_mode in ("", "INVALID", "chat_completions_extra", "responses"):
        explorers = [
            _canonical_explorer(api_mode=api_mode),
        ]
        try:
            validate_runtime(_canonical_config(explorers=explorers, synthesizer=explorers[0]["name"]))
        except BestPlanUnavailable:
            pass
        else:
            raise AssertionError(f"api_mode '{api_mode}' was accepted")


def test_invalid_reasoning_efforts_rejected():
    """reasoning_effort must be one of the allowed enum values."""
    for effort in ("", "INVALID", "超高", "maximum"):
        explorers = [
            _canonical_explorer(reasoning_effort=effort),
        ]
        try:
            validate_runtime(_canonical_config(explorers=explorers, synthesizer=explorers[0]["name"]))
        except BestPlanUnavailable:
            pass
        else:
            raise AssertionError(f"reasoning_effort '{effort}' was accepted")


def test_boolean_value_rejected_as_timeout():
    """Timeouts must accept only numeric values, not booleans."""
    for key in ("explorer_timeout", "synthesizer_timeout", "overall_timeout"):
        try:
            validate_runtime(_canonical_config(**{key: True}))
        except BestPlanUnavailable:
            pass
        else:
            raise AssertionError(f"boolean True for {key} was accepted")
        try:
            validate_runtime(_canonical_config(**{key: False}))
        except BestPlanUnavailable:
            pass
        else:
            raise AssertionError(f"boolean False for {key} was accepted")


def test_out_of_range_timeouts_rejected():
    """Timeouts must be finite and within documented bounds."""
    # explorer and synthesizer timeouts must be in 1..3600; overall in 1..7200
    # 0 and negative must fail; values above the max must fail
    for key, bound in (("explorer_timeout", 3600), ("synthesizer_timeout", 3600), ("overall_timeout", 7200)):
        try:
            validate_runtime(_canonical_config(**{key: 0}))
        except BestPlanUnavailable:
            pass
        else:
            raise AssertionError(f"{key}=0 was accepted")
        try:
            validate_runtime(_canonical_config(**{key: -1}))
        except BestPlanUnavailable:
            pass
        else:
            raise AssertionError(f"{key}=-1 was accepted")
        try:
            validate_runtime(_canonical_config(**{key: bound + 1}))
        except BestPlanUnavailable:
            pass
        else:
            raise AssertionError(f"{key}={bound + 1} was accepted")
        with pytest.raises(BestPlanUnavailable):
            validate_runtime(_canonical_config(**{key: float("nan")}))
        with pytest.raises(BestPlanUnavailable):
            validate_runtime(_canonical_config(**{key: float("inf")}))


def test_enabled_and_synthesizer_types_are_strict():
    with pytest.raises(BestPlanUnavailable):
        validate_runtime(_canonical_config(enabled=1))
    with pytest.raises(BestPlanUnavailable):
        validate_runtime(_canonical_config(synthesizer=7))
    with pytest.raises(BestPlanUnavailable):
        validate_runtime(_canonical_config(synthesizer="bad name"))


def test_unknown_top_level_keys_rejected():
    with pytest.raises(BestPlanUnavailable):
        validate_runtime(_canonical_config(bogus=True))
    with pytest.raises(BestPlanUnavailable):
        validate_runtime({**_runtime_config(), "bogus": True})


def test_ultra_requires_codex_app_server():
    """reasoning_effort 'ultra' requires api_mode 'codex_app_server'."""
    explorers = [
        _canonical_explorer(name="test", api_mode="chat_completions", reasoning_effort="ultra"),
    ]
    try:
        validate_runtime(_canonical_config(explorers=explorers, synthesizer="test"))
    except BestPlanUnavailable:
        pass
    else:
        raise AssertionError("ultra without codex_app_server was accepted")


def test_codex_app_server_requires_openai_provider():
    """api_mode 'codex_app_server' requires provider 'openai' or 'openai-codex'."""
    for provider in ("custom:neuralwatt", "anthropic", "unknown"):
        explorers = [
            _canonical_explorer(name="test", provider=provider, api_mode="codex_app_server"),
        ]
        try:
            validate_runtime(_canonical_config(explorers=explorers, synthesizer="test"))
        except BestPlanUnavailable:
            pass
        else:
            raise AssertionError(f"codex_app_server with provider '{provider}' was accepted")


def test_both_explorers_and_lanes_rejected():
    """When both `explorers` and `lanes` are present, validation must fail."""
    try:
        validate_runtime(_runtime_config(explorers=[
            _canonical_explorer(name="test"),
        ]))
    except BestPlanUnavailable:
        pass
    else:
        raise AssertionError("both explorers and lanes were accepted")


def test_legacy_lanes_normalizes_order_and_last_entry_as_synthesizer():
    """Legacy `lanes` normalizes to canonical `explorers`; last entry becomes synthesizer."""
    lanes = [
        {"name": "fast", "provider": "p", "model": "m", "api_mode": "chat_completions", "reasoning_effort": "high"},
        {"name": "slow", "provider": "p", "model": "m", "api_mode": "chat_completions", "reasoning_effort": "high"},
        {"name": "smart", "provider": "openai-codex", "model": "m", "api_mode": "codex_app_server", "reasoning_effort": "ultra"},
    ]
    cfg = validate_runtime({"lanes": lanes})
    assert [e["name"] for e in cfg["explorers"]] == ["fast", "slow", "smart"]
    assert cfg["synthesizer"] == "smart"


def test_canonical_and_legacy_both_receive_defaults():
    """When optional keys are omitted, documented defaults apply to both canonical and legacy."""
    canonical_raw = _canonical_config()
    legacy_raw = _runtime_config()
    for key in ("explorer_timeout", "synthesizer_timeout", "overall_timeout"):
        canonical_raw.pop(key)
        legacy_raw.pop(key)
    canon = validate_runtime(canonical_raw)
    legacy = validate_runtime(legacy_raw)
    for cfg in (canon, legacy):
        assert cfg.get("enabled") is True
        assert cfg["explorer_timeout"] == 180
        assert cfg["synthesizer_timeout"] == 180
        assert cfg["overall_timeout"] == 540
        assert set(cfg) == {
            "enabled", "explorers", "synthesizer", "explorer_timeout",
            "synthesizer_timeout", "overall_timeout",
        }


def test_kimi_k3_entry_matches_spec_exactly():
    """The supplied kimi-k3 explorer entry must match the spec exactly."""
    kimi = {
        "name": " KIMI-K3 ",
        "provider": " kimi-coding ",
        "model": " k3 ",
        "api_mode": " ANTHROPIC_MESSAGES ",
        "reasoning_effort": " MAX ",
    }
    cfg = validate_runtime(_canonical_config(
        explorers=[kimi],
        synthesizer=" KIMI-K3 ",
    ))
    kimi = next(e for e in cfg["explorers"] if e["name"] == "kimi-k3")
    assert kimi == {
        "name": "kimi-k3",
        "provider": "kimi-coding",
        "model": "k3",
        "api_mode": "anthropic_messages",
        "reasoning_effort": "max",
    }


def _identity(lane):
    return {
        "provider": f"resolved-{lane['provider']}",
        "model": lane["model"],
        "api_mode": lane["api_mode"],
        "base_url": f"https://{lane['name']}.invalid/v1",
        "api_key": f"{lane['name']}-secret",
    }


def test_count_and_quorum():
    assert normalize_count(1) == 2
    assert normalize_count(9) == 5
    assert [quorum_for(n) for n in range(2, 6)] == [2, 2, 3, 4]


def _default_lanes_by_name() -> dict:
    return {lane["name"]: lane for lane in DEFAULT_RUNTIME["explorers"]}


def test_default_runtime_has_two_validated_lanes():
    """DEFAULT_RUNTIME provides exactly two lanes named 'glm' and 'sol',
    each with all required keys."""
    lanes = DEFAULT_RUNTIME["explorers"]
    assert len(lanes) == 2
    names = {lane["name"] for lane in lanes}
    assert names == {"glm", "sol"}
    for lane in lanes:
        for key in _REQUIRED_LANE_KEYS:
            assert lane.get(key), f"lane '{lane.get('name')}' missing key '{key}'"


def test_validate_runtime_accepts_default_config():
    """validate_runtime() with no config must succeed (uses DEFAULT_RUNTIME)."""
    cfg = validate_runtime()
    assert len(cfg["explorers"]) == 2
    assert {lane["name"] for lane in cfg["explorers"]} == {"glm", "sol"}
    assert cfg["synthesizer"] == "sol"


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
    assert cfg["explorers"][0]["model"] == "glm-5.3-fast"
    assert cfg["explorers"][1]["model"] == "gpt-6-sol"


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
    """Legacy lane pools must contain between one and five entries."""
    with pytest.raises(BestPlanUnavailable):
        validate_runtime({"lanes": []})
    with pytest.raises(BestPlanUnavailable):
        validate_runtime({"lanes": [
            _canonical_explorer(name=f"lane-{index}") for index in range(6)
        ]})


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


def test_legacy_lane_names_are_operator_defined():
    lanes = [
        _canonical_explorer(name="fast"),
        _canonical_explorer(name="slow"),
    ]
    cfg = validate_runtime({"lanes": lanes})
    assert [entry["name"] for entry in cfg["explorers"]] == ["fast", "slow"]
    assert cfg["synthesizer"] == "slow"


def test_receipt_has_canonical_markers_and_hash():
    body = "plan body"
    attempts = [
        {
            "index": index,
            "strategy": f"strategy-{index}",
            "explorer": f"explorer-{index}",
            "configured": {"provider": "provider", "model": f"model-{index}"},
            "resolved": {"provider": "provider", "model": f"model-{index}"},
            "status": "success",
            "reason_code": None,
        }
        for index in range(3)
    ]
    synthesizer = {
        "name": "sol",
        "configured": {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        "resolved": {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        "status": "success",
        "reason_code": None,
    }
    receipt = make_receipt(
        "run-1",
        model="gpt-5.6-sol",
        quorum="3/3",
        synth_status="success",
        body=body,
        lane="sol",
        requested_count=3,
        effective_count=3,
        quorum_required=2,
        attempts=attempts,
        synthesizer=synthesizer,
    )
    assert receipt.startswith(RECEIPT_BEGIN)
    assert receipt.endswith(RECEIPT_END)
    metadata = json.loads(receipt.splitlines()[1])
    assert metadata["version"] == 2
    assert validate_receipt(receipt, body)
    assert not validate_receipt(receipt, body + "!")
    assert body_sha256(body)


def test_v2_completed_receipt_requires_successful_synthesizer():
    body = "plan body"
    attempts = [
        {
            "index": index,
            "strategy": "evidence-first",
            "explorer": "glm",
            "configured": {"provider": "zai", "model": "glm-5"},
            "resolved": {"provider": "zai", "model": "glm-5"},
            "status": "success",
            "reason_code": None,
        }
        for index in range(3)
    ]
    synthesizer = {
        "name": "sol",
        "configured": {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        "resolved": {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        "status": "failed",
        "reason_code": "provider_error",
    }
    receipt = make_receipt(
        "run-failed-synth",
        model="gpt-5.6-sol",
        quorum="3/3",
        synth_status="failed",
        body=body,
        requested_count=3,
        effective_count=3,
        quorum_required=2,
        attempts=attempts,
        synthesizer=synthesizer,
    )

    assert validate_receipt(receipt, body) is False


def test_v2_completed_receipt_requires_explorer_quorum():
    body = "plan body"
    attempts = [
        {
            "index": index,
            "strategy": "evidence-first",
            "explorer": "glm",
            "configured": {"provider": "zai", "model": "glm-5"},
            "resolved": {"provider": "zai", "model": "glm-5"},
            "status": "success" if index == 0 else "failed",
            "reason_code": None if index == 0 else "provider_error",
        }
        for index in range(3)
    ]
    synthesizer = {
        "name": "sol",
        "configured": {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        "resolved": {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        "status": "success",
        "reason_code": None,
    }
    receipt = make_receipt(
        "run-no-quorum",
        model="gpt-5.6-sol",
        quorum="1/3",
        synth_status="success",
        body=body,
        requested_count=3,
        effective_count=3,
        quorum_required=2,
        attempts=attempts,
        synthesizer=synthesizer,
    )

    assert validate_receipt(receipt, body) is False


def test_v2_failed_receipt_rejects_plan_body_hash():
    body = "plan body that must not exist on failure"
    attempts = [
        {
            "index": index,
            "strategy": "evidence-first",
            "explorer": "glm",
            "configured": {"provider": "zai", "model": "glm-5"},
            "resolved": None,
            "status": "failed",
            "reason_code": "provider_error",
        }
        for index in range(3)
    ]
    synthesizer = {
        "name": "sol",
        "configured": {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        "resolved": None,
        "status": "not_started",
        "reason_code": "quorum_unavailable",
    }
    receipt = make_receipt(
        "run-failed-with-body",
        model="gpt-5.6-sol",
        quorum="0/3",
        synth_status="not_started",
        body=body,
        requested_count=3,
        effective_count=3,
        quorum_required=2,
        attempts=attempts,
        synthesizer=synthesizer,
        status="failed",
        reason_code="quorum_unavailable",
    )

    assert validate_receipt(receipt, body) is False


def test_checked_in_v1_receipt_fixture_remains_readable():
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "bestplan_receipt_v1.json").read_text()
    )
    assert validate_receipt(fixture["receipt"], fixture["body"])
    assert not validate_receipt(fixture["receipt"], fixture["body"] + "!")


def test_v1_empty_body_hash_and_malformed_v2_validation():
    empty_hash = body_sha256("")
    v1_metadata = {
        "version": 1,
        "run_id": "legacy-empty",
        "body_sha256": empty_hash,
    }
    v1 = (
        "<<<HERMES_BESTPLAN_RECEIPT_V1>>>\n"
        + json.dumps(v1_metadata, sort_keys=True, separators=(",", ":"))
        + "\n<<<END_HERMES_BESTPLAN_RECEIPT_V1>>>"
    )
    assert validate_receipt(v1, "")

    malformed = (
        RECEIPT_BEGIN
        + "\n"
        + json.dumps({
            "version": 2,
            "run_id": "truncated",
            "body_sha256": body_sha256("body"),
        }, sort_keys=True, separators=(",", ":"))
        + "\n"
        + RECEIPT_END
    )
    assert not validate_receipt(malformed, "body")


def test_append_and_reconcile_is_idempotent(tmp_path):
    path = tmp_path / "receipts.jsonl"
    append_receipt(path, {"run_id": "run-1", "status": "running"})
    assert reconcile_bestplan_receipts(path) == ["run-1"]
    assert reconcile_bestplan_receipts(path) == []


def test_run_bestplan_uses_resolved_lane_identity_and_truthful_receipt(
    monkeypatch, tmp_path
):
    import agent.bestplan_orchestrator as orchestrator
    import run_agent

    constructed = []

    class FakeAgent:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

        def run_conversation(self, prompt):
            if "active BestPlan synthesizer" in prompt:
                return {"final_response": "final plan"}
            return {"final_response": _candidate_text()}

        def interrupt(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    monkeypatch.setattr(orchestrator, "_resolve_lane_credentials", lambda agent, lane: _identity(lane))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    result = run_bestplan(SimpleNamespace(session_id="parent"), "plan it", count=2, config=_runtime_config())

    assert result["status"] == "completed"
    assert [(item["provider"], item["model"], item["api_mode"]) for item in constructed] == [
        ("resolved-openai-codex", "configured-sol-model", "codex_app_server"),
        ("resolved-configured-glm", "configured-glm-model", "chat_completions"),
        ("resolved-openai-codex", "configured-sol-model", "codex_app_server"),
        ("resolved-openai-codex", "configured-sol-model", "codex_app_server"),
    ]
    receipt_json = json.loads(result["final_response"].splitlines()[1])
    assert set(receipt_json) == {
        "version", "run_id", "requested_count", "effective_count",
        "quorum_required", "attempts", "synthesizer", "status",
        "reason_code", "body_sha256",
    }
    assert receipt_json["version"] == 2
    assert receipt_json["status"] == "completed"
    assert receipt_json["reason_code"] is None
    assert [attempt["index"] for attempt in receipt_json["attempts"]] == [0, 1]
    assert all(set(attempt) == {
        "index", "strategy", "explorer", "configured", "resolved",
        "status", "reason_code",
    } for attempt in receipt_json["attempts"])
    assert receipt_json["synthesizer"] == {
        "name": "sol",
        "configured": {
            "provider": "openai-codex",
            "model": "configured-sol-model",
        },
        "resolved": {
            "provider": constructed[-1]["provider"],
            "model": constructed[-1]["model"],
        },
        "status": "success",
        "reason_code": None,
    }
    durable = json.loads(
        (tmp_path / "bestplan" / "receipts.jsonl").read_text().splitlines()[-1]
    )
    assert durable == receipt_json

    clamped = run_bestplan(
        SimpleNamespace(session_id="parent"),
        "plan it",
        count=0,
        config=_runtime_config(),
    )
    clamped_receipt = "\n".join(clamped["final_response"].splitlines()[:3])
    clamped_metadata = json.loads(clamped_receipt.splitlines()[1])
    assert clamped_metadata["requested_count"] == 0
    assert clamped_metadata["effective_count"] == 2
    assert validate_receipt(clamped_receipt, clamped["body"])


@pytest.mark.parametrize("pool_size", [1, 2, 3, 5])
def test_canonical_explorer_pool_cycles_in_order_and_uses_named_synthesizer(
    monkeypatch, tmp_path, pool_size
):
    import agent.bestplan_orchestrator as orchestrator
    import run_agent

    constructed_models = []

    class FakeAgent:
        def __init__(self, **kwargs):
            constructed_models.append(kwargs["model"])

        def run_conversation(self, prompt):
            if "active BestPlan synthesizer" in prompt:
                return {"final_response": "final plan"}
            return {"final_response": _candidate_text()}

        def interrupt(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

    explorers = [
        _canonical_explorer(
            name=f"explorer-{index}",
            provider=f"provider-{index}",
            model=f"model-{index}",
        )
        for index in range(pool_size)
    ]
    config = _canonical_config(
        explorers=explorers,
        synthesizer=explorers[0]["name"],
    )
    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    monkeypatch.setattr(
        orchestrator,
        "_resolve_lane_credentials",
        lambda agent, lane: _identity(lane),
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    result = run_bestplan(
        SimpleNamespace(session_id="parent"),
        "plan it",
        count=5,
        config=config,
    )

    expected_explorers = [
        explorers[index % pool_size]["model"] for index in range(5)
    ]
    assert result["status"] == "completed"
    assert constructed_models == [
        explorers[0]["model"],
        *expected_explorers,
        explorers[0]["model"],
    ]
    assert result["runtime"]["lane"] == explorers[0]["name"]


def test_synthesizer_resolution_failure_prevents_explorer_construction(
    monkeypatch, tmp_path,
):
    import agent.bestplan_orchestrator as orchestrator

    built = []
    config = _canonical_config()

    def resolve(_agent, explorer):
        if explorer["name"] == config["synthesizer"]:
            raise BestPlanUnavailable("synthetic preflight failure")
        return _identity(explorer)

    monkeypatch.setattr(orchestrator, "_resolve_lane_credentials", resolve)
    monkeypatch.setattr(
        orchestrator,
        "_build_child_agent",
        lambda parent, explorer, runtime: built.append(explorer["name"]),
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    result = run_bestplan(
        SimpleNamespace(session_id="parent"),
        "plan it",
        count=3,
        config=config,
    )

    assert result["status"] == "failed"
    assert built == []
    receipt = _latest_receipt_record(tmp_path)
    assert receipt["status"] == "failed"
    assert receipt["reason_code"] == "credential_unavailable"
    assert receipt["synthesizer"]["status"] == "not_started"
    assert receipt["body_sha256"] is None
    assert all(attempt["status"] == "failed" for attempt in receipt["attempts"])


def test_synthesizer_construction_failure_prevents_explorer_construction(
    monkeypatch, tmp_path,
):
    import agent.bestplan_orchestrator as orchestrator

    built = []
    config = _canonical_config()

    monkeypatch.setattr(
        orchestrator,
        "_resolve_lane_credentials",
        lambda agent, explorer: _identity(explorer),
    )

    def build(_parent, explorer, _runtime):
        built.append(explorer["name"])
        if explorer["name"] == config["synthesizer"]:
            raise RuntimeError("synthetic construction failure")
        raise AssertionError("explorer constructed before synth preflight")

    monkeypatch.setattr(orchestrator, "_build_child_agent", build)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    result = run_bestplan(
        SimpleNamespace(session_id="parent"),
        "plan it",
        count=3,
        config=config,
    )

    assert result["status"] == "failed"
    assert built == [config["synthesizer"]]
    receipt = _latest_receipt_record(tmp_path)
    assert receipt["reason_code"] == "construction_failed"
    assert receipt["synthesizer"]["status"] == "not_started"


def test_unavailable_kimi_attempt_is_ordered_and_not_substituted(
    monkeypatch, tmp_path
):
    import agent.bestplan_orchestrator as orchestrator
    import run_agent

    constructed = []
    config = _canonical_config()

    class FakeAgent:
        def __init__(self, **kwargs):
            constructed.append(kwargs["model"])

        def run_conversation(self, prompt):
            if "active BestPlan synthesizer" in prompt:
                return {"final_response": "final plan"}
            return {"final_response": _candidate_text()}

        def interrupt(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

    def resolve(_agent, explorer):
        if explorer["name"] == "kimi-k3":
            raise BestPlanUnavailable("synthetic Kimi outage")
        return _identity(explorer)

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    monkeypatch.setattr(orchestrator, "_resolve_lane_credentials", resolve)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    result = run_bestplan(
        SimpleNamespace(session_id="parent"),
        "plan it",
        count=3,
        config=config,
    )

    assert result["status"] == "completed"
    assert constructed == [
        "configured-sol-model",
        "configured-glm-model",
        "configured-sol-model",
        "configured-sol-model",
    ]
    assert [
        (attempt["index"], attempt["explorer"], attempt["status"])
        for attempt in result["attempts"]
    ] == [
        (0, "glm", "success"),
        (1, "kimi-k3", "failed"),
        (2, "sol", "success"),
    ]
    assert result["attempts"][1]["reason_code"] == "credential_unavailable"


def test_out_of_order_completion_preserves_attempt_and_candidate_order(
    monkeypatch, tmp_path
):
    import agent.bestplan_orchestrator as orchestrator
    import run_agent

    synth_prompts = []
    delays = {
        "configured-glm-model": 0.03,
        "configured-kimi-model": 0.01,
        "configured-sol-model": 0.02,
    }

    class FakeAgent:
        def __init__(self, **kwargs):
            self.model = kwargs["model"]

        def run_conversation(self, prompt):
            if "active BestPlan synthesizer" in prompt:
                synth_prompts.append(prompt)
                return {"final_response": "final plan"}
            time.sleep(delays[self.model])
            return {"final_response": _candidate_text(self.model)}

        def interrupt(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    monkeypatch.setattr(
        orchestrator,
        "_resolve_lane_credentials",
        lambda agent, explorer: _identity(explorer),
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    result = run_bestplan(
        SimpleNamespace(session_id="parent"),
        "plan it",
        count=3,
        config=_canonical_config(),
    )

    assert result["status"] == "completed"
    assert [attempt["explorer"] for attempt in result["attempts"]] == [
        "glm", "kimi-k3", "sol",
    ]
    packet = synth_prompts[0]
    positions = [
        packet.index(f'"summary": "{model}"')
        for model in (
            "configured-glm-model",
            "configured-kimi-model",
            "configured-sol-model",
        )
    ]
    assert positions == sorted(positions)


def test_quorum_failure_persists_terminal_v2_receipt(monkeypatch, tmp_path):
    import agent.bestplan_orchestrator as orchestrator
    import run_agent

    class FakeAgent:
        def __init__(self, **kwargs):
            self.model = kwargs["model"]

        def run_conversation(self, prompt):
            return {"final_response": _candidate_text(self.model)}

        def interrupt(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

    def resolve(_agent, explorer):
        if explorer["name"] != "sol":
            raise BestPlanUnavailable("synthetic outage")
        return _identity(explorer)

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    monkeypatch.setattr(orchestrator, "_resolve_lane_credentials", resolve)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    result = run_bestplan(
        SimpleNamespace(session_id="parent"),
        "plan it",
        count=3,
        config=_canonical_config(),
    )

    assert result["status"] == "failed"
    assert "body" not in result
    receipt = _latest_receipt_record(tmp_path)
    assert receipt["reason_code"] == "quorum_unavailable"
    assert receipt["synthesizer"]["status"] == "not_started"
    assert receipt["synthesizer"]["reason_code"] == "quorum_unavailable"
    assert [attempt["status"] for attempt in receipt["attempts"]] == [
        "failed", "failed", "success",
    ]


def test_synthesizer_provider_failure_persists_no_plan_body(
    monkeypatch, tmp_path
):
    import agent.bestplan_orchestrator as orchestrator
    import run_agent

    synth_runs = 0

    class FakeAgent:
        def __init__(self, **kwargs):
            pass

        def run_conversation(self, prompt):
            nonlocal synth_runs
            if "active BestPlan synthesizer" in prompt:
                synth_runs += 1
                raise RuntimeError("synthetic provider failure")
            return {"final_response": _candidate_text()}

        def interrupt(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    monkeypatch.setattr(
        orchestrator,
        "_resolve_lane_credentials",
        lambda agent, explorer: _identity(explorer),
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    result = run_bestplan(
        SimpleNamespace(session_id="parent"),
        "plan it",
        count=3,
        config=_canonical_config(),
    )

    assert synth_runs == 1
    assert result["status"] == "failed"
    assert "body" not in result
    assert "final_response" not in result
    receipt = _latest_receipt_record(tmp_path)
    assert receipt["status"] == "failed"
    assert receipt["reason_code"] == "synthesizer_failed"
    assert receipt["synthesizer"]["status"] == "failed"
    assert receipt["synthesizer"]["reason_code"] == "provider_error"
    assert receipt["body_sha256"] is None


@pytest.mark.parametrize(
    ("failure_type", "expected_status", "expected_reason"),
    [
        (RuntimeError, "failed", "provider_error"),
        (TimeoutError, "timeout", "timeout"),
    ],
)
def test_explorer_provider_failure_uses_stable_reason(
    monkeypatch, tmp_path, failure_type, expected_status, expected_reason
):
    import agent.bestplan_orchestrator as orchestrator
    import run_agent

    class FakeAgent:
        def __init__(self, **kwargs):
            self.model = kwargs["model"]

        def run_conversation(self, prompt):
            if "active BestPlan synthesizer" in prompt:
                return {"final_response": "final plan"}
            if self.model == "configured-kimi-model":
                raise failure_type("SENTINEL_SECRET")
            return {"final_response": _candidate_text(self.model)}

        def interrupt(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    monkeypatch.setattr(
        orchestrator,
        "_resolve_lane_credentials",
        lambda _agent, explorer: _identity(explorer),
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    result = run_bestplan(
        SimpleNamespace(session_id="parent"),
        "plan it",
        count=3,
        config=_canonical_config(),
    )

    assert result["status"] == "completed"
    assert result["attempts"][1]["status"] == expected_status
    assert result["attempts"][1]["reason_code"] == expected_reason
    assert "SENTINEL_SECRET" not in json.dumps(result, sort_keys=True)


def test_terminal_failure_never_exposes_provider_exception_secret(
    monkeypatch, tmp_path, caplog
):
    import agent.bestplan_orchestrator as orchestrator

    sentinel = "SENTINEL_SECRET"

    def fail_resolution(_agent, _explorer):
        raise BestPlanUnavailable(f"credential rejected: {sentinel}")

    monkeypatch.setattr(orchestrator, "_resolve_lane_credentials", fail_resolution)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with caplog.at_level("WARNING"):
        result = run_bestplan(
            SimpleNamespace(session_id="parent"),
            "plan it",
            count=3,
            config=_canonical_config(),
        )

    durable = (tmp_path / "bestplan" / "receipts.jsonl").read_text()
    assert sentinel not in json.dumps(result, sort_keys=True)
    assert sentinel not in durable
    assert sentinel not in caplog.text
    assert result["reason_code"] == "credential_unavailable"


def test_receipt_persistence_failure_never_logs_exception_secret(
    monkeypatch, tmp_path, caplog
):
    import agent.bestplan_orchestrator as orchestrator

    sentinel = "SENTINEL_SECRET"
    monkeypatch.setattr(
        orchestrator,
        "_resolve_lane_credentials",
        lambda *_args: (_ for _ in ()).throw(
            BestPlanUnavailable(f"credential rejected: {sentinel}")
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "append_receipt",
        lambda *_args: (_ for _ in ()).throw(OSError(sentinel)),
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with caplog.at_level("ERROR"):
        result = run_bestplan(
            SimpleNamespace(session_id="parent"),
            "plan it",
            count=3,
            config=_canonical_config(),
        )

    assert sentinel not in json.dumps(result, sort_keys=True)
    assert sentinel not in caplog.text
    assert result["reason_code"] == "receipt_persistence_failed"
    assert result["receipt_persisted"] is False


def test_success_receipt_persistence_failure_fails_closed_without_plan(
    monkeypatch, tmp_path
):
    import agent.bestplan_orchestrator as orchestrator
    import run_agent

    class FakeAgent:
        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, prompt):
            if "active BestPlan synthesizer" in prompt:
                return {"final_response": "unsigned plan must not escape"}
            return {"final_response": _candidate_text()}

        def interrupt(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

    real_append = orchestrator.append_receipt
    append_calls = 0

    def fail_once_then_append(path, record):
        nonlocal append_calls
        append_calls += 1
        if append_calls == 1:
            raise OSError("SENTINEL_SECRET")
        real_append(path, record)

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    monkeypatch.setattr(
        orchestrator,
        "_resolve_lane_credentials",
        lambda _agent, explorer: _identity(explorer),
    )
    monkeypatch.setattr(orchestrator, "append_receipt", fail_once_then_append)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    result = run_bestplan(
        SimpleNamespace(session_id="parent"),
        "plan it",
        count=3,
        config=_canonical_config(),
    )

    assert append_calls == 2
    assert result["status"] == "failed"
    assert result["reason_code"] == "receipt_persistence_failed"
    assert "body" not in result
    assert "final_response" not in result
    receipt = _latest_receipt_record(tmp_path)
    assert receipt["status"] == "failed"
    assert receipt["reason_code"] == "receipt_persistence_failed"
    assert receipt["synthesizer"]["status"] == "success"
    assert receipt["synthesizer"]["reason_code"] is None
    assert receipt["body_sha256"] is None


def test_v2_receipt_rejects_non_allowlisted_reason_code():
    attempts = [
        {
            "index": index,
            "strategy": "evidence-first",
            "explorer": "glm",
            "configured": {"provider": "zai", "model": "glm-5"},
            "resolved": None,
            "status": "failed",
            "reason_code": "SENTINEL_SECRET",
        }
        for index in range(3)
    ]
    synthesizer = {
        "name": "sol",
        "configured": {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        "resolved": None,
        "status": "not_started",
        "reason_code": "SENTINEL_SECRET",
    }
    receipt = make_receipt(
        "run-secret-reason",
        model="gpt-5.6-sol",
        provider="openai-codex",
        api_mode="codex_app_server",
        quorum="0/3",
        synth_status="not_started",
        body="",
        requested_count=3,
        effective_count=3,
        quorum_required=2,
        attempts=attempts,
        synthesizer=synthesizer,
        status="failed",
        reason_code="SENTINEL_SECRET",
    )

    assert validate_receipt(receipt, "") is False


def test_lane_credential_resolution_uses_configured_provider_model_and_endpoint(
    monkeypatch,
):
    import agent.bestplan_orchestrator as orchestrator
    from hermes_cli import runtime_provider

    captured = {}

    def fake_resolve(**kwargs):
        captured.update(kwargs)
        return {
            "provider": "resolved-wire-provider",
            "api_mode": "runtime-default-that-must-not-override-lane",
            "base_url": "https://resolved.invalid/v1",
            "api_key": "resolved-secret",
        }

    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", fake_resolve)
    lane = {
        "name": "glm",
        "provider": "configured-provider",
        "model": "configured-model",
        "api_mode": "chat_completions",
        "reasoning_effort": "high",
        "base_url": "https://configured.invalid/v1",
        "api_key": "configured-secret",
    }

    identity = orchestrator._resolve_lane_credentials(SimpleNamespace(), lane)

    assert captured == {
        "requested": "configured-provider",
        "explicit_api_key": "configured-secret",
        "explicit_base_url": "https://configured.invalid/v1",
        "target_model": "configured-model",
    }
    assert identity["provider"] == "resolved-wire-provider"
    assert identity["model"] == "configured-model"
    assert identity["api_mode"] == "chat_completions"
    assert identity["base_url"] == "https://resolved.invalid/v1"
    assert identity["api_key"] == "resolved-secret"


def test_kimi_k3_resolution_rejects_legacy_moonshot_endpoint(monkeypatch):
    import agent.bestplan_orchestrator as orchestrator
    from hermes_cli import runtime_provider

    monkeypatch.setattr(
        runtime_provider,
        "resolve_runtime_provider",
        lambda **_kwargs: {
            "provider": "kimi-coding",
            "api_mode": "chat_completions",
            "base_url": "https://api.moonshot.ai/v1",
            "api_key": "legacy-secret",
        },
    )
    lane = {
        "name": "kimi-k3",
        "provider": "kimi-coding",
        "model": "k3",
        "api_mode": "anthropic_messages",
        "reasoning_effort": "max",
    }

    with pytest.raises(BestPlanUnavailable):
        orchestrator._resolve_lane_credentials(SimpleNamespace(), lane)


def test_kimi_k3_resolution_accepts_exact_coding_plan_endpoint(monkeypatch):
    import agent.bestplan_orchestrator as orchestrator
    from hermes_cli import runtime_provider

    monkeypatch.setattr(
        runtime_provider,
        "resolve_runtime_provider",
        lambda **_kwargs: {
            "provider": "kimi-coding",
            "api_mode": "anthropic_messages",
            "base_url": "https://api.kimi.com/coding",
            "api_key": "sk-kimi-SENTINEL",
        },
    )
    lane = {
        "name": "kimi-k3",
        "provider": "kimi-coding",
        "model": "k3",
        "api_mode": "anthropic_messages",
        "reasoning_effort": "max",
    }

    identity = orchestrator._resolve_lane_credentials(SimpleNamespace(), lane)

    assert identity["base_url"] == "https://api.kimi.com/coding"
    assert identity["api_mode"] == "anthropic_messages"
    assert identity["model"] == "k3"


def test_parallel_explorers_build_sequentially_and_restore_tool_global(monkeypatch):
    import agent.bestplan_orchestrator as orchestrator
    import model_tools
    import run_agent

    lock = threading.Lock()
    active_builds = 0
    max_active_builds = 0
    original = ["parent-tool"]
    model_tools._last_resolved_tool_names = list(original)

    class FakeAgent:
        def __init__(self, **kwargs):
            nonlocal active_builds, max_active_builds
            with lock:
                active_builds += 1
                max_active_builds = max(max_active_builds, active_builds)
            model_tools._last_resolved_tool_names = [kwargs["model"]]
            time.sleep(0.025)
            with lock:
                active_builds -= 1

        def run_conversation(self, prompt):
            if "active BestPlan synthesizer" in prompt:
                return {"final_response": "final plan"}
            return {"final_response": _candidate_text()}

        def interrupt(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    monkeypatch.setattr(orchestrator, "_resolve_lane_credentials", lambda agent, lane: _identity(lane))

    result = run_bestplan(SimpleNamespace(session_id="parent"), "plan it", count=2, config=_runtime_config())

    assert result["status"] == "completed"
    assert max_active_builds == 1
    assert model_tools._last_resolved_tool_names == original


def test_explorer_timeout_interrupts_and_closes_live_provider_call(monkeypatch):
    import agent.bestplan_orchestrator as orchestrator
    import run_agent

    instances = []

    class FakeAgent:
        def __init__(self, **kwargs):
            self.model = kwargs["model"]
            self.stop = threading.Event()
            self.active = False
            instances.append(self)

        def run_conversation(self, prompt):
            if self.model == "configured-glm-model":
                self.active = True
                self.stop.wait(0.4)
                self.active = False
                return {"final_response": _candidate_text("late")}
            return {"final_response": _candidate_text()}

        def interrupt(self, *_args, **_kwargs):
            self.stop.set()

        def close(self):
            self.stop.set()

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    monkeypatch.setattr(orchestrator, "_resolve_lane_credentials", lambda agent, lane: _identity(lane))
    _allow_subsecond_runtime_for_timing_tests(monkeypatch, orchestrator)

    started = time.monotonic()

    result = run_bestplan(
        SimpleNamespace(session_id="parent"),
        "plan it",
        count=2,
        config=_runtime_config(explorer_timeout=0.03, overall_timeout=0.2),
    )

    assert time.monotonic() - started < 0.2
    assert result["status"] == "failed"
    assert all(not instance.active for instance in instances)
    assert all(instance.stop.is_set() for instance in instances)


def test_explorer_timeout_joins_provider_unwind_before_return(monkeypatch):
    """Interrupt is only a signal; BestPlan must join the active provider."""
    import agent.bestplan_orchestrator as orchestrator
    import run_agent

    instances = []

    class SlowUnwindAgent:
        def __init__(self, **kwargs):
            self.model = kwargs["model"]
            self.stop = threading.Event()
            self.active = False
            instances.append(self)

        def run_conversation(self, prompt):
            if self.model == "configured-glm-model":
                self.active = True
                self.stop.wait()
                time.sleep(0.12)
                self.active = False
                return {"final_response": _candidate_text("late")}
            return {"final_response": _candidate_text()}

        def interrupt(self, *_args, **_kwargs):
            self.stop.set()

        def close(self):
            self.stop.set()

    monkeypatch.setattr(run_agent, "AIAgent", SlowUnwindAgent)
    monkeypatch.setattr(
        orchestrator,
        "_resolve_lane_credentials",
        lambda agent, lane: _identity(lane),
    )
    _allow_subsecond_runtime_for_timing_tests(monkeypatch, orchestrator)

    result = run_bestplan(
        SimpleNamespace(session_id="parent"),
        "plan it",
        count=2,
        config=_runtime_config(explorer_timeout=0.02, overall_timeout=0.5),
    )

    assert result["status"] == "failed"
    assert all(not instance.active for instance in instances)


def test_hostile_provider_cannot_block_bestplan_past_hard_teardown_deadline(
    monkeypatch,
):
    """Python threads are not killable; a hostile transport must be detached."""
    import agent.bestplan_orchestrator as orchestrator
    import run_agent

    release = threading.Event()
    instances = []

    class HostileAgent:
        def __init__(self, **kwargs):
            self.active = False
            self.request_aborted = False
            self.sockets_forced = False
            self.client_closed = False
            self.codex_killed = False

            owner = self

            class _HttpClient:
                def close(self):
                    owner.client_closed = True

            class _CodexClient:
                def close(self, timeout=0):
                    owner.codex_killed = True

            self.client = _HttpClient()
            self._codex_session = SimpleNamespace(_client=_CodexClient())
            self._active_request_abort = lambda reason: setattr(
                self, "request_aborted", True
            )
            self._force_close_tcp_sockets = lambda client: setattr(
                self, "sockets_forced", True
            )
            instances.append(self)

        def run_conversation(self, prompt):
            self.active = True
            release.wait()
            self.active = False
            return {"final_response": _candidate_text("late")}

        def interrupt(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent, "AIAgent", HostileAgent)
    monkeypatch.setattr(
        orchestrator,
        "_resolve_lane_credentials",
        lambda agent, lane: _identity(lane),
    )
    _allow_subsecond_runtime_for_timing_tests(monkeypatch, orchestrator)
    monkeypatch.setattr(orchestrator, "_CHILD_CLEANUP_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(
        orchestrator,
        "_CHILD_CLEANUP_HARD_SECONDS",
        0.05,
        raising=False,
    )
    safety_release = threading.Timer(0.8, release.set)
    safety_release.daemon = True
    safety_release.start()
    started = time.monotonic()
    try:
        result = run_bestplan(
            SimpleNamespace(session_id="parent"),
            "plan it",
            count=2,
            config=_runtime_config(explorer_timeout=0.01, overall_timeout=0.1),
        )
        elapsed = time.monotonic() - started

        assert result["status"] == "failed"
        assert result["cleanup_incomplete"] is True
        assert elapsed < 0.2
        active_instances = [instance for instance in instances if instance.active]
        assert active_instances
        assert all(instance.request_aborted for instance in active_instances)
        assert all(instance.sockets_forced for instance in active_instances)
        assert all(instance.client_closed for instance in active_instances)
        assert all(instance.codex_killed for instance in active_instances)
    finally:
        release.set()
        safety_release.cancel()


def test_synthesizer_timeout_interrupts_and_closes_live_provider_call(monkeypatch):
    import agent.bestplan_orchestrator as orchestrator
    import run_agent

    instances = []

    class FakeAgent:
        def __init__(self, **kwargs):
            self.stop = threading.Event()
            self.active = False
            instances.append(self)

        def run_conversation(self, prompt):
            if "active BestPlan synthesizer" in prompt:
                self.active = True
                self.stop.wait(0.4)
                self.active = False
                return {"final_response": "late plan"}
            return {"final_response": _candidate_text()}

        def interrupt(self, *_args, **_kwargs):
            self.stop.set()

        def close(self):
            self.stop.set()

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    monkeypatch.setattr(orchestrator, "_resolve_lane_credentials", lambda agent, lane: _identity(lane))
    _allow_subsecond_runtime_for_timing_tests(monkeypatch, orchestrator)
    started = time.monotonic()

    result = run_bestplan(
        SimpleNamespace(session_id="parent"),
        "plan it",
        count=2,
        config=_runtime_config(synthesizer_timeout=0.03, overall_timeout=0.2),
    )

    assert time.monotonic() - started < 0.2
    assert result["status"] == "failed"
    assert "synthesizer" in result["error"].lower()
    assert all(not instance.active for instance in instances)
    assert all(instance.stop.is_set() for instance in instances)


def test_overall_timeout_bounds_explorer_pool_without_shutdown_join(monkeypatch):
    import agent.bestplan_orchestrator as orchestrator
    import run_agent

    instances = []

    class FakeAgent:
        def __init__(self, **kwargs):
            self.stop = threading.Event()
            self.active = False
            instances.append(self)

        def run_conversation(self, prompt):
            self.active = True
            self.stop.wait(0.4)
            self.active = False
            return {"final_response": _candidate_text("late")}

        def interrupt(self, *_args, **_kwargs):
            self.stop.set()

        def close(self):
            self.stop.set()

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    monkeypatch.setattr(orchestrator, "_resolve_lane_credentials", lambda agent, lane: _identity(lane))
    _allow_subsecond_runtime_for_timing_tests(monkeypatch, orchestrator)
    started = time.monotonic()

    result = run_bestplan(
        SimpleNamespace(session_id="parent"),
        "plan it",
        count=2,
        config=_runtime_config(explorer_timeout=1.0, overall_timeout=0.03),
    )

    assert time.monotonic() - started < 0.2
    assert result["status"] == "failed"
    assert "overall" in result["error"].lower()
    assert all(not instance.active for instance in instances)
    assert all(instance.stop.is_set() for instance in instances)
