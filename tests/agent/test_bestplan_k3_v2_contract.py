"""Focused contract tests for canonical BestPlan K3/V2 orchestration."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.bestplan_orchestrator import (
    BestPlanUnavailable,
    RECEIPT_BEGIN,
    RECEIPT_END,
    make_receipt,
    run_bestplan,
    validate_receipt,
    validate_runtime,
)


def _explorer(name: str, provider: str, model: str) -> dict[str, str]:
    return {
        "name": name,
        "provider": provider,
        "model": model,
        "api_mode": "chat_completions",
        "reasoning_effort": "high",
    }


def _config(**overrides) -> dict:
    explorers = [
        _explorer("alpha", "provider-alpha", "model-alpha"),
        _explorer("beta", "provider-beta", "model-beta"),
        {
            "name": "sol",
            "provider": "openai-codex",
            "model": "model-sol",
            "api_mode": "codex_app_server",
            "reasoning_effort": "ultra",
        },
    ]
    config = {
        "enabled": True,
        "explorers": explorers,
        "synthesizer": "sol",
        "explorer_timeout": 1,
        "synthesizer_timeout": 1,
        "overall_timeout": 5,
    }
    config.update(overrides)
    return config


def _identity(explorer: dict[str, str]) -> dict[str, str]:
    return {
        "provider": explorer["provider"],
        "requested_provider": explorer["provider"],
        "model": explorer["model"],
        "api_mode": explorer["api_mode"],
        "base_url": f"https://{explorer['name']}.invalid/v1",
        "api_key": "SENTINEL_SECRET",
    }


def _candidate(label: str) -> str:
    return "HERMES_BESTPLAN_CANDIDATE_V1\n" + json.dumps(
        {
            "schema": "HERMES_BESTPLAN_CANDIDATE_V1",
            "summary": label,
            "steps": ["step"],
            "risks": ["risk"],
            "verification": ["verify"],
        }
    )


def _plan(workspace: str) -> str:
    manifest = {
        "version": 1,
        "mode": "delegate",
        "risk": "low",
        "slices": [
            {
                "id": "implement",
                "kind": "implement",
                "goal": "Implement the requested change.",
                "depends_on": [],
                "capability": "fast_fallback",
                "workspace": workspace,
                "allowed_paths": ["src/"],
                "read_only": False,
                "expected_artifacts": ["src/result.txt"],
                "acceptance": ["The requested work is verified."],
            }
        ],
        "merge_policy": "Verify before integration.",
        "stop_condition": "Acceptance passes.",
        "escalation_predicates": [],
    }
    return (
        "<<<HERMES_BESTPLAN_V1>>>\n"
        + json.dumps(manifest, sort_keys=True)
        + "\n<<<END_HERMES_BESTPLAN_V1>>>"
    )


def _receipt_record(home: Path) -> dict:
    lines = (home / "bestplan" / "receipts.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(lines) == 1
    return json.loads(lines[0])


def test_runtime_requires_only_canonical_explorers_and_named_synthesizer():
    resolved = validate_runtime(_config())
    assert [entry["name"] for entry in resolved["explorers"]] == [
        "alpha",
        "beta",
        "sol",
    ]
    assert resolved["synthesizer"] == "sol"

    with pytest.raises(BestPlanUnavailable):
        validate_runtime({"lanes": _config()["explorers"]})
    with pytest.raises(BestPlanUnavailable):
        validate_runtime({**_config(), "synthesizer": "missing"})
    with pytest.raises(BestPlanUnavailable):
        validate_runtime({**_config(), "unknown": "SENTINEL_SECRET"})


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "other"},
        {"provider": "moonshot"},
        {"model": "kimi-k3"},
        {"api_mode": "chat_completions"},
        {"reasoning_effort": "high"},
    ],
)
def test_optional_k3_explorer_must_match_exact_identity_tuple(overrides):
    kimi = {
        "name": "kimi-k3",
        "provider": "kimi-coding",
        "model": "k3",
        "api_mode": "anthropic_messages",
        "reasoning_effort": "max",
    }
    kimi.update(overrides)
    with pytest.raises(BestPlanUnavailable):
        validate_runtime(_config(explorers=[kimi], synthesizer=kimi["name"]))

    exact = validate_runtime(
        _config(
            explorers=[
                {
                    "name": " KIMI-K3 ",
                    "provider": " kimi-coding ",
                    "model": " k3 ",
                    "api_mode": " ANTHROPIC_MESSAGES ",
                    "reasoning_effort": " MAX ",
                }
            ],
            synthesizer=" KIMI-K3 ",
        )
    )
    assert exact["explorers"] == [
        {
            "name": "kimi-k3",
            "provider": "kimi-coding",
            "model": "k3",
            "api_mode": "anthropic_messages",
            "reasoning_effort": "max",
        }
    ]


def test_non_k3_kimi_explorer_remains_an_arbitrary_valid_lane():
    explorer = {
        "name": "kimi-k2",
        "provider": "kimi-coding",
        "model": "kimi-k2.5",
        "api_mode": "anthropic_messages",
        "reasoning_effort": "high",
    }
    resolved = validate_runtime(
        _config(explorers=[explorer], synthesizer="kimi-k2")
    )
    assert resolved["explorers"] == [explorer]


@pytest.mark.parametrize(
    "key,value",
    [
        ("explorer_timeout", True),
        ("explorer_timeout", float("nan")),
        ("explorer_timeout", 10**400),
        ("synthesizer_timeout", float("inf")),
        ("synthesizer_timeout", 3601),
        ("overall_timeout", 7201),
    ],
)
def test_runtime_rejects_nonfinite_boolean_and_out_of_range_timeouts(key, value):
    with pytest.raises(BestPlanUnavailable):
        validate_runtime(_config(**{key: value}))


@pytest.mark.parametrize(
    "runtime_patch",
    [
        {"provider": "moonshot"},
        {"model": "other"},
        {"api_mode": "chat_completions"},
        {"base_url": "https://api.moonshot.ai/v1"},
    ],
)
def test_k3_runtime_rejects_post_resolution_identity_or_endpoint_drift(
    monkeypatch, runtime_patch
):
    from hermes_cli import runtime_provider
    import agent.bestplan_orchestrator as orchestrator

    runtime = {
        "provider": "kimi-coding",
        "model": "k3",
        "api_mode": "anthropic_messages",
        "base_url": "https://api.kimi.com/coding",
        "api_key": "SENTINEL_SECRET",
    }
    runtime.update(runtime_patch)
    monkeypatch.setattr(
        runtime_provider, "resolve_runtime_provider", lambda **_kwargs: runtime
    )
    kimi = {
        "name": "kimi-k3",
        "provider": "kimi-coding",
        "model": "k3",
        "api_mode": "anthropic_messages",
        "reasoning_effort": "max",
    }

    with pytest.raises(orchestrator.BestPlanRuntimeInvalid):
        orchestrator._resolve_lane_credentials(SimpleNamespace(), kimi)


def test_v2_receipt_rejects_completed_without_quorum_and_failed_with_body():
    attempts = [
        {
            "index": index,
            "strategy": "evidence-first",
            "explorer": f"explorer-{index}",
            "configured": {"provider": "provider", "model": f"model-{index}"},
            "resolved": {"provider": "provider", "model": f"model-{index}"},
            "status": "success" if index == 0 else "failed",
            "reason_code": None if index == 0 else "provider_error",
        }
        for index in range(3)
    ]
    synthesizer = {
        "name": "sol",
        "configured": {"provider": "openai-codex", "model": "model-sol"},
        "resolved": {"provider": "openai-codex", "model": "model-sol"},
        "status": "success",
        "reason_code": None,
    }
    receipt = make_receipt(
        "run-no-quorum",
        model="model-sol",
        quorum="1/3",
        synth_status="success",
        body="body",
        requested_count=3,
        effective_count=3,
        quorum_required=2,
        attempts=attempts,
        synthesizer=synthesizer,
    )
    assert receipt.startswith(RECEIPT_BEGIN)
    assert receipt.endswith(RECEIPT_END)
    assert validate_receipt(receipt, "body") is False

    failed = make_receipt(
        "run-failed",
        model="model-sol",
        quorum="1/3",
        synth_status="not_started",
        body="forbidden body",
        requested_count=3,
        effective_count=3,
        quorum_required=2,
        attempts=attempts,
        synthesizer={**synthesizer, "status": "not_started", "reason_code": "quorum_unavailable"},
        status="failed",
        reason_code="quorum_unavailable",
    )
    assert validate_receipt(failed, "forbidden body") is False


def test_ordered_attempts_require_quorum_before_named_synthesis(
    monkeypatch, tmp_path
):
    import agent.bestplan_orchestrator as orchestrator
    import run_agent

    synth_calls = 0

    class FakeAgent:
        def __init__(self, **kwargs):
            self.model = kwargs["model"]

        def run_conversation(self, prompt):
            nonlocal synth_calls
            if "active BestPlan synthesizer" in prompt:
                synth_calls += 1
                return {"final_response": _plan(str(tmp_path))}
            if self.model != "model-alpha":
                raise RuntimeError("SENTINEL_SECRET")
            return {"final_response": _candidate(self.model)}

        def clear_interrupt(self):
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
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    result = run_bestplan(
        SimpleNamespace(session_id="parent"), "plan it", count=3, config=_config()
    )

    assert result["status"] == "failed"
    assert result["reason_code"] == "quorum_unavailable"
    assert synth_calls == 0
    assert [attempt["index"] for attempt in result["attempts"]] == [0, 1, 2]
    assert [attempt["explorer"] for attempt in result["attempts"]] == [
        "alpha",
        "beta",
        "sol",
    ]
    assert "body" not in result
    assert "final_response" not in result
    assert "SENTINEL_SECRET" not in json.dumps(result, sort_keys=True)
    assert validate_receipt(result["receipt"], "")
    durable = _receipt_record(tmp_path)
    assert durable["version"] == 2
    assert durable["status"] == "failed"
    assert durable["body_sha256"] is None


def test_only_named_synthesizer_runs_and_terminal_receipt_appends_once(
    monkeypatch, tmp_path
):
    import agent.bestplan_orchestrator as orchestrator
    import run_agent

    synth_models: list[str] = []
    synth_prompts: list[str] = []
    delays = {"model-alpha": 0.03, "model-beta": 0.01, "model-sol": 0.02}

    class FakeAgent:
        def __init__(self, **kwargs):
            self.model = kwargs["model"]

        def run_conversation(self, prompt):
            if "active BestPlan synthesizer" in prompt:
                synth_models.append(self.model)
                synth_prompts.append(prompt)
                return {"final_response": _plan(str(tmp_path))}
            time.sleep(delays[self.model])
            return {"final_response": _candidate(self.model)}

        def clear_interrupt(self):
            pass

        def close(self):
            pass

    append_calls: list[dict] = []
    original_append = orchestrator.append_receipt

    def record_append(path, record):
        append_calls.append(record)
        original_append(path, record)

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    monkeypatch.setattr(
        orchestrator,
        "_resolve_lane_credentials",
        lambda _agent, explorer: _identity(explorer),
    )
    monkeypatch.setattr(orchestrator, "append_receipt", record_append)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    result = run_bestplan(
        SimpleNamespace(session_id="parent"), "plan it", count=3, config=_config()
    )

    assert result["status"] == "completed"
    assert synth_models == ["model-sol"]
    assert len(append_calls) == 1
    assert [attempt["explorer"] for attempt in result["attempts"]] == [
        "alpha",
        "beta",
        "sol",
    ]
    candidate_positions = [
        synth_prompts[0].index(f'"summary": "model-{name}"')
        for name in ("alpha", "beta", "sol")
    ]
    assert candidate_positions == sorted(candidate_positions)
    receipt = "\n".join(result["final_response"].splitlines()[:3])
    assert validate_receipt(receipt, result["body"])
    assert _receipt_record(tmp_path) == json.loads(receipt.splitlines()[1])


def test_successful_body_survives_receipt_write_failure_with_sanitized_warning(
    monkeypatch, tmp_path
):
    import agent.bestplan_orchestrator as orchestrator
    import run_agent

    class FakeAgent:
        def __init__(self, **kwargs):
            self.model = kwargs["model"]

        def run_conversation(self, prompt):
            if "active BestPlan synthesizer" in prompt:
                return {"final_response": _plan(str(tmp_path))}
            return {"final_response": _candidate(self.model)}

        def clear_interrupt(self):
            pass

        def close(self):
            pass

    append_count = 0

    def fail_append(_path, _record):
        nonlocal append_count
        append_count += 1
        raise OSError("SENTINEL_SECRET")

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    monkeypatch.setattr(
        orchestrator,
        "_resolve_lane_credentials",
        lambda _agent, explorer: _identity(explorer),
    )
    monkeypatch.setattr(orchestrator, "append_receipt", fail_append)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    result = run_bestplan(
        SimpleNamespace(session_id="parent"), "plan it", count=3, config=_config()
    )

    assert append_count == 1
    assert result["status"] == "completed"
    assert result["receipt_persisted"] is False
    assert result["warning_reason_code"] == "receipt_persistence_failed"
    assert result["body"] in result["final_response"]
    assert "receipt persistence failed" in result["final_response"].lower()
    assert "SENTINEL_SECRET" not in json.dumps(result, sort_keys=True)


def test_synthesizer_preflight_failure_is_sanitized_and_terminalized_once(
    monkeypatch, tmp_path
):
    import agent.bestplan_orchestrator as orchestrator

    sentinel = "SENTINEL_SECRET"
    builds = 0
    append_calls = 0

    def fail_resolution(_agent, _explorer):
        raise BestPlanUnavailable(f"credential rejected: {sentinel}")

    def build_should_not_run(*_args):
        nonlocal builds
        builds += 1
        raise AssertionError("construction ran after failed preflight resolution")

    original_append = orchestrator.append_receipt

    def record_append(path, record):
        nonlocal append_calls
        append_calls += 1
        original_append(path, record)

    monkeypatch.setattr(orchestrator, "_resolve_lane_credentials", fail_resolution)
    monkeypatch.setattr(orchestrator, "_build_child_agent", build_should_not_run)
    monkeypatch.setattr(orchestrator, "append_receipt", record_append)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    result = run_bestplan(
        SimpleNamespace(session_id="parent"), "plan it", count=3, config=_config()
    )

    assert builds == 0
    assert append_calls == 1
    assert result["status"] == "failed"
    assert result["reason_code"] == "credential_unavailable"
    assert "body" not in result
    assert validate_receipt(result["receipt"], "")
    assert sentinel not in json.dumps(result, sort_keys=True)
    assert sentinel not in json.dumps(_receipt_record(tmp_path), sort_keys=True)


def test_failed_receipt_write_reports_persistence_reason_without_secret(
    monkeypatch, tmp_path, caplog
):
    import agent.bestplan_orchestrator as orchestrator

    sentinel = "SENTINEL_SECRET"
    monkeypatch.setattr(
        orchestrator,
        "_resolve_lane_credentials",
        lambda *_args: (_ for _ in ()).throw(BestPlanUnavailable(sentinel)),
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
            config=_config(),
        )

    assert result["status"] == "failed"
    assert result["reason_code"] == "receipt_persistence_failed"
    assert result["receipt_persisted"] is False
    assert sentinel not in json.dumps(result, sort_keys=True)
    assert sentinel not in caplog.text


def test_parallel_child_construction_is_serial_and_restores_tool_global(
    monkeypatch, tmp_path
):
    import agent.bestplan_orchestrator as orchestrator
    import model_tools
    import run_agent

    state_lock = threading.Lock()
    active_builds = 0
    max_active_builds = 0
    original = ["parent-tool"]
    model_tools.set_last_resolved_tool_names(original)

    class FakeAgent:
        def __init__(self, **kwargs):
            nonlocal active_builds, max_active_builds
            with state_lock:
                active_builds += 1
                max_active_builds = max(max_active_builds, active_builds)
            model_tools.set_last_resolved_tool_names([kwargs["model"]])
            time.sleep(0.01)
            self.model = kwargs["model"]
            with state_lock:
                active_builds -= 1

        def run_conversation(self, prompt):
            if "active BestPlan synthesizer" in prompt:
                return {"final_response": _plan(str(tmp_path))}
            return {"final_response": _candidate(self.model)}

        def clear_interrupt(self):
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
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    result = run_bestplan(
        SimpleNamespace(session_id="parent"), "plan it", count=3, config=_config()
    )

    assert result["status"] == "completed"
    assert max_active_builds == 1
    assert model_tools.get_last_resolved_tool_names() == original


def test_tool_state_guard_does_not_clobber_a_concurrent_later_publication():
    import model_tools

    original = model_tools.get_last_resolved_tool_names()
    model_tools.set_last_resolved_tool_names(["parent-before"])
    child_active = threading.Event()
    release_child = threading.Event()
    later_published = threading.Event()

    def temporary_child_build():
        with model_tools.preserve_last_resolved_tool_names():
            model_tools.set_last_resolved_tool_names(["temporary-child"])
            child_active.set()
            release_child.wait(1)

    def publish_later_parent():
        child_active.wait(1)
        model_tools.set_last_resolved_tool_names(["parent-after"])
        later_published.set()

    child_thread = threading.Thread(target=temporary_child_build)
    parent_thread = threading.Thread(target=publish_later_parent)
    child_thread.start()
    parent_thread.start()
    try:
        assert child_active.wait(1)
        assert not later_published.wait(0.05)
    finally:
        release_child.set()
        child_thread.join(1)
        parent_thread.join(1)

    assert not child_thread.is_alive()
    assert not parent_thread.is_alive()
    assert later_published.is_set()
    assert model_tools.get_last_resolved_tool_names() == ["parent-after"]
    model_tools.set_last_resolved_tool_names(original)
