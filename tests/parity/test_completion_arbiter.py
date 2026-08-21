"""Completion-arbiter seam (migration bridge, Phase 6 / Option A-lite)."""
from __future__ import annotations

import types
import importlib.util

import agent.turn_finalizer as _tf
from hermes_cli import plugins as _plugins

# Reuse the canonical FakeAgent from the upstream finalizer tests so the seam
# test exercises the REAL finalizer path identically to existing regressions.
_spec = importlib.util.spec_from_file_location(
    "_fp_fakeagent",
    "tests/agent/test_turn_finalizer_final_response_persistence.py",
)
_fp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fp)
FakeAgent = _fp.FakeAgent

MESSAGES = [
    {"role": "user", "content": "do it"},
    {"role": "assistant", "content": "I'll check.", "tool_calls": [
        {"id": "call-1", "function": {"name": "terminal", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "call-1", "name": "terminal", "content": "ok"},
]


def _run(agent, turn_id="turn"):
    return _tf.finalize_turn(
        agent, final_response="Done.", api_call_count=2, interrupted=False,
        failed=False, messages=[dict(m) for m in MESSAGES],
        conversation_history=[], effective_task_id="task", turn_id=turn_id,
        user_message="do it", original_user_message="do it",
        _should_review_memory=False, _turn_exit_reason="fallback_prior_turn_content",
    )


def test_no_claim_noop(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_k: [])
    _plugins._COMPLETION_ARBITER.clear()
    res = _run(FakeAgent())
    assert isinstance(res, dict) and res["final_response"] == "Done."
    assert "turn" not in _plugins._COMPLETION_ARBITER


def test_arbiter_receives_finalized_result(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_k: [])
    _plugins._COMPLETION_ARBITER.clear()
    received = []
    _plugins._register_completion_arbiter(
        "turn", "bp", lambda res, **kw: received.append((res, kw)))
    res = _run(FakeAgent())
    assert received, "commit_fn must be called"
    got, kw = received[0]
    assert kw.get("turn_id") == "turn"
    assert got is res
    assert "turn" not in _plugins._COMPLETION_ARBITER  # released


def test_double_claim_rejected(monkeypatch):
    _plugins._COMPLETION_ARBITER.clear()
    assert _plugins._register_completion_arbiter("t", "a", lambda *a, **k: None) is True
    assert _plugins._register_completion_arbiter("t", "b", lambda *a, **k: None) is False


def test_arbiter_failure_does_not_break_finalize(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_k: [])
    _plugins._COMPLETION_ARBITER.clear()
    def boom(res, **kw): raise RuntimeError("arbiter failure")
    _plugins._register_completion_arbiter("turn", "bp", boom)
    res = _run(FakeAgent())  # must not raise
    assert res["final_response"] == "Done."
    assert "turn" not in _plugins._COMPLETION_ARBITER
