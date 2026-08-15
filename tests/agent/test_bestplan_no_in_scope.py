"""Regression tests for the non-executable BestPlan synthesis outcome."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


NO_IN_SCOPE_REJECTION = json.dumps(
    {
        "schema": "HERMES_BESTPLAN_NO_IN_SCOPE_V1",
        "reason_code": "no_in_scope_implementation",
    },
    sort_keys=True,
    separators=(",", ":"),
)


def _runtime_config(lane):
    required = ("name", "provider", "model", "api_mode", "reasoning_effort")
    configured = {key: lane[key] for key in required}
    return {
        "explorers": [configured],
        "synthesizer": lane["name"],
        "explorer_timeout": 1.0,
        "synthesizer_timeout": 1.0,
        "overall_timeout": 2.0,
    }


def _identity(lane):
    return {
        "provider": f"resolved-{lane['provider']}",
        "requested_provider": lane["provider"],
        "model": lane["model"],
        "api_mode": lane["api_mode"],
        "base_url": f"https://{lane['name']}.invalid/v1",
        "api_key": f"{lane['name']}-secret",
    }


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


def _manifest(workspace):
    return {
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
                "workspace": str(workspace),
                "allowed_paths": ["src/"],
                "read_only": False,
                "expected_artifacts": ["src/result.txt"],
                "acceptance": ["pytest -q -- tests/path.py::test_name"],
            }
        ],
        "merge_policy": "Verify before integration.",
        "stop_condition": "Acceptance passes.",
        "escalation_predicates": ["security_sensitive_request"],
    }


def _run_synthesis(monkeypatch, tmp_path, *, lane, synthesis, repair=None):
    import agent.bestplan_orchestrator as orchestrator
    import run_agent
    from agent.bestplan_orchestrator import run_bestplan

    calls = []

    class FakeAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.tools = []
            self.valid_tool_names = set()

        def run_conversation(self, prompt, **_kwargs):
            calls.append(
                {
                    "prompt": prompt,
                    "schema": getattr(self, "_bestplan_output_schema", None),
                }
            )
            if "BestPlan envelope repair" in prompt:
                if repair is None:
                    pytest.fail("the typed outcome must not enter envelope repair")
                return {"final_response": repair}
            if "active BestPlan synthesizer" in prompt:
                return {"final_response": synthesis}
            return {"final_response": _candidate_text(self.kwargs["model"])}

        def interrupt(self, *_args, **_kwargs):
            pass

        def clear_interrupt(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    monkeypatch.setattr(
        orchestrator,
        "_resolve_lane_credentials",
        lambda _agent, configured, **_kwargs: _identity(configured),
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    result = run_bestplan(
        SimpleNamespace(session_id="parent"),
        "Double check and give me the plan",
        count=2,
        config=_runtime_config(lane),
    )
    return result, calls


def _assert_no_scope_failure(result):
    from agent.bestplan_orchestrator import _valid_v2_receipt_metadata

    assert result["status"] == "failed"
    assert result["reason_code"] == "no_in_scope_implementation"
    assert "final_response" not in result
    assert result.get("body") in (None, "")
    metadata = result["bestplan_receipt_metadata"]
    assert metadata["status"] == "failed"
    assert metadata["reason_code"] == "no_in_scope_implementation"
    assert metadata["synthesizer"]["status"] == "success"
    assert _valid_v2_receipt_metadata(metadata, "") is True


def test_unstructured_no_scope_skips_repair_and_returns_failed_receipt(
    monkeypatch, tmp_path
):
    lane = {
        "name": "local",
        "provider": "provider-a",
        "model": "local-model",
        "api_mode": "chat_completions",
        "reasoning_effort": "high",
    }

    result, calls = _run_synthesis(
        monkeypatch,
        tmp_path,
        lane=lane,
        synthesis=NO_IN_SCOPE_REJECTION,
    )

    _assert_no_scope_failure(result)
    assert not any("BestPlan envelope repair" in call["prompt"] for call in calls)


@pytest.mark.parametrize(
    "candidate",
    [
        NO_IN_SCOPE_REJECTION + "\nprose",
        json.dumps(
            {
                "schema": "HERMES_BESTPLAN_NO_IN_SCOPE_V1",
                "reason_code": "no_in_scope_implementation",
                "reason": "outside /tmp/other",
            }
        ),
        json.dumps(
            {
                "schema": "HERMES_BESTPLAN_NO_IN_SCOPE_V1",
                "reason_code": "workspace_mismatch",
            }
        ),
        (
            '{"schema":"HERMES_BESTPLAN_NO_IN_SCOPE_V1",'
            '"schema":"HERMES_BESTPLAN_NO_IN_SCOPE_V1",'
            '"reason_code":"no_in_scope_implementation"}'
        ),
    ],
)
def test_unstructured_no_scope_rejects_prose_extra_keys_and_other_reasons(candidate):
    from agent.bestplan_orchestrator import _recognized_no_in_scope_output

    assert _recognized_no_in_scope_output(candidate) is False


@pytest.mark.parametrize(
    "candidate",
    [
        NO_IN_SCOPE_REJECTION,
        json.dumps(
            {
                "schema": "HERMES_BESTPLAN_SYNTHESIS_V1",
                "outcome": "no_in_scope_implementation",
                "manifest": {},
            }
        ),
        json.dumps(
            {
                "schema": "HERMES_BESTPLAN_SYNTHESIS_V1",
                "outcome": "executable_plan",
                "manifest": None,
            }
        ),
        json.dumps(
            {
                "schema": "HERMES_BESTPLAN_SYNTHESIS_V1",
                "outcome": "no_in_scope_implementation",
                "manifest": None,
                "reason": "outside /tmp/other",
            }
        ),
        (
            '{"schema":"HERMES_BESTPLAN_SYNTHESIS_V1",'
            '"outcome":"no_in_scope_implementation",'
            '"outcome":"no_in_scope_implementation","manifest":null}'
        ),
    ],
)
def test_structured_synthesis_rejects_cross_field_mismatches_and_extra_keys(
    candidate,
):
    from agent.bestplan_orchestrator import _parsed_structured_synthesis

    assert _parsed_structured_synthesis(candidate) is None


def test_repair_can_return_the_exact_unstructured_no_scope_outcome(
    monkeypatch, tmp_path
):
    lane = {
        "name": "local",
        "provider": "provider-a",
        "model": "local-model",
        "api_mode": "chat_completions",
        "reasoning_effort": "high",
    }

    result, calls = _run_synthesis(
        monkeypatch,
        tmp_path,
        lane=lane,
        synthesis="invalid synthesis",
        repair=NO_IN_SCOPE_REJECTION,
    )

    _assert_no_scope_failure(result)
    assert sum("BestPlan envelope repair" in call["prompt"] for call in calls) == 1


def test_codex_structured_no_scope_uses_null_manifest_and_skips_repair(
    monkeypatch, tmp_path
):
    lane = {
        "name": "sol",
        "provider": "openai-codex",
        "model": "gpt-5.6-sol",
        "api_mode": "codex_app_server",
        "reasoning_effort": "ultra",
    }
    synthesis = json.dumps(
        {
            "schema": "HERMES_BESTPLAN_SYNTHESIS_V1",
            "outcome": "no_in_scope_implementation",
            "manifest": None,
        }
    )

    result, calls = _run_synthesis(
        monkeypatch,
        tmp_path,
        lane=lane,
        synthesis=synthesis,
    )

    _assert_no_scope_failure(result)
    synth_call = next(
        call for call in calls if "active BestPlan synthesizer" in call["prompt"]
    )
    assert synth_call["schema"]["type"] == "object"
    assert "uniqueItems" not in json.dumps(synth_call["schema"])
    assert "manifest=null" in synth_call["prompt"]
    assert not any("BestPlan envelope repair" in call["prompt"] for call in calls)


def test_codex_structured_executable_plan_is_unwrapped_and_host_validated(
    monkeypatch, tmp_path
):
    lane = {
        "name": "sol",
        "provider": "openai-codex",
        "model": "gpt-5.6-sol",
        "api_mode": "codex_app_server",
        "reasoning_effort": "ultra",
    }
    manifest = _manifest(tmp_path)
    synthesis = json.dumps(
        {
            "schema": "HERMES_BESTPLAN_SYNTHESIS_V1",
            "outcome": "executable_plan",
            "manifest": manifest,
        }
    )

    result, _calls = _run_synthesis(
        monkeypatch,
        tmp_path,
        lane=lane,
        synthesis=synthesis,
    )

    assert result["status"] == "completed"
    assert result["body"].startswith("<<<HERMES_BESTPLAN_V1>>>\n")
    authority = json.loads(result["body"].splitlines()[1])
    assert authority == {"version": 1, "manifest": manifest}


def test_codex_structured_executable_plan_cannot_change_workspace(
    monkeypatch, tmp_path
):
    lane = {
        "name": "sol",
        "provider": "openai-codex",
        "model": "gpt-5.6-sol",
        "api_mode": "codex_app_server",
        "reasoning_effort": "ultra",
    }
    manifest = _manifest(tmp_path / "outside")
    synthesis = json.dumps(
        {
            "schema": "HERMES_BESTPLAN_SYNTHESIS_V1",
            "outcome": "executable_plan",
            "manifest": manifest,
        }
    )

    result, calls = _run_synthesis(
        monkeypatch,
        tmp_path,
        lane=lane,
        synthesis=synthesis,
        repair="unused",
    )

    assert result["status"] == "failed"
    assert result["reason_code"] == "synthesizer_failed"
    assert not any("BestPlan envelope repair" in call["prompt"] for call in calls)


def test_codex_structured_transport_rejects_raw_unstructured_no_scope(
    monkeypatch, tmp_path
):
    lane = {
        "name": "sol",
        "provider": "openai-codex",
        "model": "gpt-5.6-sol",
        "api_mode": "codex_app_server",
        "reasoning_effort": "ultra",
    }

    result, calls = _run_synthesis(
        monkeypatch,
        tmp_path,
        lane=lane,
        synthesis=NO_IN_SCOPE_REJECTION,
        repair="unused",
    )

    assert result["status"] == "failed"
    assert result["reason_code"] == "synthesizer_failed"
    assert not any("BestPlan envelope repair" in call["prompt"] for call in calls)


def test_render_bestplan_failure_for_no_scope_is_fixed_and_echo_free():
    from agent.bestplan_state import render_bestplan_failure

    response = render_bestplan_failure(
        {
            "error": "No implementation fits /tmp/stale-hermes-home",
            "reason_code": "no_in_scope_implementation",
        }
    )

    assert response.startswith("BestPlan unavailable")
    assert "active workspace" in response
    assert "Select the intended project workspace" in response
    assert "/tmp/stale-hermes-home" not in response
    assert "No plan was created or executed." in response
    assert "Approval" not in response
    assert "Bestplan executable receipt" not in response
    assert "bare `go`" not in response


def _failed_receipt_metadata():
    from agent.bestplan_orchestrator import make_receipt

    attempts = [
        {
            "index": index,
            "strategy": strategy,
            "explorer": f"explorer-{index}",
            "configured": {"provider": "provider-a", "model": "model-a"},
            "resolved": {"provider": "provider-a", "model": "model-a"},
            "status": "success",
            "reason_code": None,
        }
        for index, strategy in enumerate(("evidence-first", "counterfactual"))
    ]
    synthesizer = {
        "name": "sol",
        "configured": {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        "resolved": {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        "status": "success",
        "reason_code": None,
    }
    receipt = make_receipt(
        "run-no-in-scope",
        model="gpt-5.6-sol",
        quorum="2/2",
        synth_status="success",
        body="",
        requested_count=2,
        effective_count=2,
        quorum_required=2,
        attempts=attempts,
        synthesizer=synthesizer,
        status="failed",
        reason_code="no_in_scope_implementation",
    )
    return json.loads(receipt.splitlines()[1])


@pytest.mark.parametrize("failure", ["no_quorum", "synthesizer_failed"])
def test_no_scope_receipt_requires_quorum_and_successful_synthesizer(failure):
    from agent.bestplan_orchestrator import _valid_v2_receipt_metadata
    from agent.bestplan_state import _valid_no_scope_receipt_metadata

    metadata = _failed_receipt_metadata()
    if failure == "no_quorum":
        metadata["attempts"][0]["status"] = "failed"
        metadata["attempts"][0]["reason_code"] = "provider_error"
    else:
        metadata["synthesizer"]["status"] = "failed"
        metadata["synthesizer"]["reason_code"] = "provider_error"

    assert _valid_v2_receipt_metadata(metadata, "") is True
    assert _valid_no_scope_receipt_metadata(metadata) is False


def _executable_response(workspace):
    manifest = _manifest(workspace)
    return (
        "<<<HERMES_BESTPLAN_V1>>>\n"
        + json.dumps({"version": 1, "manifest": manifest}, sort_keys=True)
        + "\n<<<END_HERMES_BESTPLAN_V1>>>"
    )


@pytest.mark.parametrize(
    ("prior_state", "expected_state"),
    [
        ("provisional", "rejected"),
        ("pending", "rejected"),
        ("approved", "rejected"),
        ("running", "running"),
        ("waiting", "waiting"),
    ],
)
def test_valid_no_scope_supersedes_only_unstarted_matching_plan(
    monkeypatch, tmp_path, prior_state, expected_state
):
    import agent.bestplan_state as state
    from agent.bestplan_state import (
        BestplanStore,
        capture_bestplan_agent_result,
        capture_bestplan_response,
    )

    workspace = tmp_path / "work"
    workspace.mkdir()
    store = BestplanStore(db_path=tmp_path / "state.db")
    provisional = prior_state == "provisional"
    prior = capture_bestplan_response(
        _executable_response(workspace),
        session_id="no-scope-session",
        profile="coder",
        workspace=str(workspace),
        baseline_fingerprint="base-1",
        store=store,
        provisional=provisional,
    )
    assert prior.executable is True
    if prior_state == "approved":
        assert store.approve_plan(prior.plan_id)
    elif prior_state in {"running", "waiting"}:
        claimed = store.prepare_dispatch_intent(
            prior.plan_id,
            "base-1",
            resolved_runtimes=[{
                "route": "code_worker",
                "provider": "test",
                "model": "coder",
            }],
            session_id="no-scope-session",
            profile="coder",
            workspace=str(workspace),
        )
        assert claimed is not None
        if prior_state == "waiting":
            assert store.record_dispatch(
                prior.plan_id,
                delegation_ids=[f"bestplan-{prior.plan_id}"],
            )

    response = "BestPlan unavailable\n\n- No plan was created or executed."
    result = {
        "final_response": response,
        "messages": [{"role": "assistant", "content": response}],
        "failed": True,
        "turn_exit_reason": "bestplan",
        "bestplan_receipt_metadata": _failed_receipt_metadata(),
    }
    monkeypatch.setattr(
        state,
        "capture_bestplan_response",
        lambda *_args, **_kwargs: pytest.fail(
            "a validated no-scope failure must bypass executable capture"
        ),
    )

    try:
        updated = capture_bestplan_agent_result(
            result,
            invocation_message="/bestplan double check",
            session_id="no-scope-session",
            profile="coder",
            workspace=str(workspace),
            baseline_fingerprint="base-1",
            store=store,
            host_agent=SimpleNamespace(
                _persist_session=lambda *_args, **_kwargs: True
            ),
            provisional=True,
            local_execution=True,
        )

        assert updated["bestplan_capture"]["executable"] is False
        assert store.get_plan(prior.plan_id)["state"] == expected_state
        if prior_state == "provisional":
            assert store.commit_provisional_plan(prior.plan_id) is False
    finally:
        store.close()


def test_no_scope_supersession_is_ordered_and_exact_context_bound(
    monkeypatch, tmp_path
):
    import agent.bestplan_state as state
    from agent.bestplan_state import BestplanStore, capture_bestplan_response

    workspace = tmp_path / "work"
    other_workspace = tmp_path / "other-work"
    workspace.mkdir()
    other_workspace.mkdir()
    store = BestplanStore(db_path=tmp_path / "state.db")

    def capture(*, at, session="s1", profile="coder", root=workspace, baseline="base-1"):
        monkeypatch.setattr(state.time, "time", lambda: at)
        return capture_bestplan_response(
            _executable_response(root),
            session_id=session,
            profile=profile,
            workspace=str(root),
            baseline_fingerprint=baseline,
            store=store,
        )

    try:
        older_pending = capture(at=100.0)
        older_approved = capture(at=110.0)
        assert store.approve_plan(older_approved.plan_id)
        other_baseline = capture(at=120.0, baseline="base-2")
        other_session = capture(at=130.0, session="s2")
        other_profile = capture(at=140.0, profile="reviewer")
        other_root = capture(at=145.0, root=other_workspace)

        changed = store.supersede_unstarted_plans(
            session_id="s1",
            profile="coder",
            workspace=str(workspace),
            baseline_fingerprint="base-1",
            before=200.0,
            local_execution=False,
        )

        assert changed == 2
        assert store.get_plan(older_pending.plan_id)["state"] == "rejected"
        assert store.get_plan(older_approved.plan_id)["state"] == "rejected"
        for untouched in (
            other_baseline,
            other_session,
            other_profile,
            other_root,
        ):
            assert store.get_plan(untouched.plan_id)["state"] == "pending"

        newer = capture(at=300.0)
        assert store.get_plan(newer.plan_id)["state"] == "pending"
    finally:
        store.close()


@pytest.mark.parametrize("failure", ["no_quorum", "synthesizer_failed"])
def test_inconsistent_no_scope_receipt_cannot_supersede_pending_plan(
    tmp_path, failure
):
    from agent.bestplan_state import (
        BestplanStore,
        capture_bestplan_agent_result,
        capture_bestplan_response,
    )

    workspace = tmp_path / "work"
    workspace.mkdir()
    store = BestplanStore(db_path=tmp_path / "state.db")
    prior = capture_bestplan_response(
        _executable_response(workspace),
        session_id="no-scope-session",
        profile="coder",
        workspace=str(workspace),
        baseline_fingerprint="base-1",
        store=store,
    )
    metadata = _failed_receipt_metadata()
    if failure == "no_quorum":
        metadata["attempts"][0]["status"] = "failed"
        metadata["attempts"][0]["reason_code"] = "provider_error"
    else:
        metadata["synthesizer"]["status"] = "failed"
        metadata["synthesizer"]["reason_code"] = "provider_error"
    response = "BestPlan unavailable\n\n- No plan was created or executed."

    try:
        updated = capture_bestplan_agent_result(
            {
                "final_response": response,
                "messages": [{"role": "assistant", "content": response}],
                "failed": True,
                "turn_exit_reason": "bestplan",
                "bestplan_receipt_metadata": metadata,
            },
            invocation_message="/bestplan double check",
            session_id="no-scope-session",
            profile="coder",
            workspace=str(workspace),
            baseline_fingerprint="base-1",
            store=store,
            host_agent=SimpleNamespace(
                _persist_session=lambda *_args, **_kwargs: True
            ),
            provisional=True,
            local_execution=True,
        )

        assert updated["bestplan_capture"]["executable"] is False
        assert updated["bestplan_capture"]["error"] is not None
        assert store.get_plan(prior.plan_id)["state"] == "pending"
    finally:
        store.close()


@pytest.mark.parametrize("failure_mode", ["cleanup_error", "persist_false"])
def test_unpersisted_no_scope_response_cannot_supersede_pending_plan(
    tmp_path, failure_mode
):
    from agent.bestplan_state import (
        BestplanStore,
        capture_bestplan_agent_result,
        capture_bestplan_response,
    )

    workspace = tmp_path / "work"
    workspace.mkdir()
    store = BestplanStore(db_path=tmp_path / "state.db")
    prior = capture_bestplan_response(
        _executable_response(workspace),
        session_id="no-scope-session",
        profile="coder",
        workspace=str(workspace),
        baseline_fingerprint="base-1",
        store=store,
    )
    response = "BestPlan unavailable\n\n- No plan was created or executed."
    result = {
        "final_response": response,
        "messages": [{"role": "assistant", "content": response}],
        "failed": True,
        "turn_exit_reason": "bestplan",
        "bestplan_receipt_metadata": _failed_receipt_metadata(),
    }
    if failure_mode == "cleanup_error":
        result["cleanup_errors"] = ["persist_session: disk full"]
    host_agent = SimpleNamespace(
        _persist_session=lambda *_args, **_kwargs: failure_mode != "persist_false"
    )

    try:
        with pytest.raises(
            RuntimeError,
            match=r"no-scope response persistence (?:failed|unavailable)",
        ):
            capture_bestplan_agent_result(
                result,
                invocation_message="/bestplan double check",
                session_id="no-scope-session",
                profile="coder",
                workspace=str(workspace),
                baseline_fingerprint="base-1",
                store=store,
                host_agent=host_agent,
                provisional=True,
                local_execution=True,
            )

        assert store.get_plan(prior.plan_id)["state"] == "pending"
    finally:
        store.close()


def test_shared_capture_bypasses_plan_store_for_valid_failed_no_scope_receipt(
    monkeypatch, tmp_path
):
    import agent.bestplan_state as state
    from agent.bestplan_state import (
        BestplanStore,
        capture_bestplan_agent_result,
        try_resolve_go,
    )

    workspace = tmp_path / "work"
    workspace.mkdir()
    store = BestplanStore(db_path=tmp_path / "state.db")
    response = (
        "BestPlan unavailable\n\n"
        "- Reason: The active workspace cannot contain an executable implementation "
        "for this task.\n\n- No plan was created or executed."
    )
    messages = [
        {"role": "user", "content": "/bestplan double check"},
        {"role": "assistant", "content": response},
    ]
    result = {
        "final_response": response,
        "messages": messages,
        "failed": True,
        "turn_exit_reason": "bestplan",
        "bestplan_receipt_metadata": _failed_receipt_metadata(),
    }

    monkeypatch.setattr(
        state,
        "capture_bestplan_response",
        lambda *_args, **_kwargs: pytest.fail(
            "a validated no-scope failure must bypass executable capture"
        ),
    )

    try:
        updated = capture_bestplan_agent_result(
            result,
            invocation_message="/bestplan double check",
            session_id="no-scope-session",
            profile="coder",
            workspace=str(workspace),
            baseline_fingerprint="base-1",
            store=store,
            host_agent=SimpleNamespace(
                _persist_session=lambda *_args, **_kwargs: True
            ),
            provisional=True,
            local_execution=True,
        )

        assert updated["final_response"] == response
        assert updated["messages"] == messages
        assert updated["bestplan_capture"] == {
            "executable": False,
            "plan_id": None,
            "digest": None,
            "error": "no_in_scope_implementation",
        }
        assert store.list_for_session("no-scope-session") == []

        resolved = try_resolve_go(
            "go",
            session_id="no-scope-session",
            workspace=str(workspace),
            profile="coder",
            parent_agent=SimpleNamespace(),
            config={"autonomy": {"go_enabled": True}},
            store=store,
        )
        assert resolved.status == "no_plan"
    finally:
        store.close()
