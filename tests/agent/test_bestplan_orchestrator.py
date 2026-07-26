"""Tests for agent.bestplan_orchestrator.

These assert *invariants* (lane structure, safety constraints, receipt
integrity) rather than snapshot literal model strings.  Model strings are
config-owned and change when SOTA models are updated; the contracts below
must hold regardless of which model names are configured.
"""

import json
import threading
import time
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


def _synth_plan_envelope():
    manifest = {
        "version": 1,
        "mode": "delegate",
        "risk": "low",
        "slices": [{
            "id": "implement",
            "kind": "implement",
            "goal": "Implement the requested change.",
            "depends_on": [],
            "capability": "fast_fallback",
            "workspace": "/tmp/work",
            "allowed_paths": ["src/"],
            "read_only": False,
            "expected_artifacts": ["src/result.txt"],
            "acceptance": ["The requested change is verified."],
        }],
        "merge_policy": "Verify before integration.",
        "stop_condition": "Acceptance passes.",
        "escalation_predicates": ["security_sensitive_request"],
    }
    return (
        "<<<HERMES_BESTPLAN_V1>>>\n"
        + json.dumps(manifest, sort_keys=True)
        + "\n<<<END_HERMES_BESTPLAN_V1>>>"
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
                "provider": "configured-sol",
                "model": "configured-sol-model",
                "api_mode": "codex_app_server",
                "reasoning_effort": "ultra",
            },
        ],
        "explorer_timeout": 0.04,
        "synthesizer_timeout": 0.04,
        "overall_timeout": 0.12,
    }
    config.update(overrides)
    return config


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


def test_validate_runtime_accepts_legacy_keyed_lane_mapping():
    """Existing config.yaml files may key lane definitions by lane name."""
    keyed_lanes = {
        "glm": {
            "provider": "neuralwatt",
            "model": "glm-5.2",
            "api_mode": "chat_completions",
            "reasoning_effort": "high",
        },
        "sol": {
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
            "api_mode": "codex_app_server",
            "reasoning_effort": "ultra",
        },
    }

    cfg = validate_runtime({"lanes": keyed_lanes})

    assert [lane["name"] for lane in cfg["lanes"]] == ["glm", "sol"]
    assert cfg["lanes"][0]["provider"] == "neuralwatt"
    assert cfg["lanes"][1]["model"] == "gpt-5.6-sol"


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


def test_synthesizer_envelope_must_pass_exact_v1_capture_constraints():
    import agent.bestplan_orchestrator as orchestrator

    manifest = json.loads(
        _synth_plan_envelope().splitlines()[1]
    )
    second = dict(manifest["slices"][0])
    second.update(
        {
            "id": "dependent",
            "goal": "Run after the first slice.",
            "depends_on": ["implement"],
        }
    )
    manifest["slices"].append(second)
    body = (
        "<<<HERMES_BESTPLAN_V1>>>\n"
        + json.dumps(manifest)
        + "\n<<<END_HERMES_BESTPLAN_V1>>>"
    )

    assert orchestrator._validated_plan_envelope(
        body, workspace="/tmp/work"
    ) is None


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
                return {"final_response": _synth_plan_envelope()}
            return {"final_response": _candidate_text()}

        def interrupt(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    monkeypatch.setattr(orchestrator, "_resolve_lane_credentials", lambda agent, lane: _identity(lane))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_CWD", "/tmp/work")

    result = run_bestplan(SimpleNamespace(session_id="parent"), "plan it", count=2, config=_runtime_config())

    assert result["status"] == "completed"
    assert [(item["provider"], item["model"], item["api_mode"]) for item in constructed] == [
        ("resolved-configured-glm", "configured-glm-model", "chat_completions"),
        ("resolved-configured-sol", "configured-sol-model", "codex_app_server"),
        ("resolved-configured-sol", "configured-sol-model", "codex_app_server"),
    ]
    receipt_json = json.loads(result["final_response"].splitlines()[1])
    assert receipt_json["provider"] == constructed[-1]["provider"]
    assert receipt_json["model"] == constructed[-1]["model"]
    assert receipt_json["api_mode"] == constructed[-1]["api_mode"]
    durable = json.loads(
        (tmp_path / "bestplan" / "receipts.jsonl").read_text().splitlines()[-1]
    )
    assert durable["provider"] == receipt_json["provider"]
    assert durable["model"] == receipt_json["model"]
    assert durable["api_mode"] == receipt_json["api_mode"]
    from agent.bestplan_state import BestplanStore, capture_bestplan_response

    capture = capture_bestplan_response(
        result["final_response"],
        session_id="webui-session",
        profile="coder",
        workspace="/tmp/work",
        baseline_fingerprint="base",
        store=BestplanStore(db_path=tmp_path / "authority.db"),
    )
    assert capture.executable is True
    assert "Authoritative executable manifest" in capture.response
    assert "HERMES_BESTPLAN" not in capture.response


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
                return {"final_response": _synth_plan_envelope()}
            return {"final_response": _candidate_text()}

        def interrupt(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    monkeypatch.setattr(orchestrator, "_resolve_lane_credentials", lambda agent, lane: _identity(lane))
    monkeypatch.setenv("TERMINAL_CWD", "/tmp/work")

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
        assert any(instance.active for instance in instances)
        assert all(instance.request_aborted for instance in instances)
        assert all(instance.sockets_forced for instance in instances)
        assert all(instance.client_closed for instance in instances)
        assert all(instance.codex_killed for instance in instances)
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
