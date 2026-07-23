"""Aggregate admission gates for autonomous background review requests."""

from __future__ import annotations

from types import SimpleNamespace

import agent.background_review as background_review


def test_incident_669788_tokens_is_rejected_before_provider_submission(monkeypatch):
    monkeypatch.setattr(
        background_review,
        "estimate_request_tokens_rough",
        lambda *_args, **_kwargs: 661_596,
    )

    receipt = background_review._background_review_admission_receipt(
        context_length=262_144,
        system_prompt="system",
        history=[{"role": "user", "content": "captured history"}],
        user_prompt="review",
        tools=[{"type": "function", "function": {"name": "memory"}}],
        requested_output_tokens=8_192,
        routed=True,
        provider="nous",
        model="incident-model",
    )

    assert receipt["prompt_tokens"] == 661_596
    assert receipt["requested_output_tokens"] == 8_192
    assert receipt["raw_requested_tokens"] == 669_788
    assert receipt["context_length"] == 262_144
    assert receipt["admitted"] is False
    assert receipt["reason"] == "aggregate_context_budget_exceeded"


def test_small_review_is_admitted_with_output_and_synthesis_reserve(monkeypatch):
    monkeypatch.setattr(
        background_review,
        "estimate_request_tokens_rough",
        lambda *_args, **_kwargs: 10_000,
    )

    receipt = background_review._background_review_admission_receipt(
        context_length=262_144,
        system_prompt="system",
        history=[],
        user_prompt="review",
        tools=[],
        requested_output_tokens=4_096,
        routed=False,
        provider="openai",
        model="small",
    )

    assert receipt["admitted"] is True
    assert receipt["synthesis_reserve_tokens"] > 0
    assert receipt["admission_tokens"] >= (
        receipt["raw_requested_tokens"] + receipt["synthesis_reserve_tokens"]
    )


def test_routed_history_digest_is_copy_only_deterministic_and_bounded():
    huge = "0123456789" * 30_000
    original = [
        {"role": "assistant", "tool_calls": [{"id": "call-1"}]},
        {"role": "tool", "tool_call_id": "call-1", "content": huge},
        {"role": "user", "content": "continue"},
    ]

    first = background_review._bound_routed_review_history(
        original,
        context_length=65_536,
    )
    second = background_review._bound_routed_review_history(
        original,
        context_length=65_536,
    )

    assert first == second
    assert original[1]["content"] == huge
    assert len(first[1]["content"]) < 4_000
    assert "sha256=" in first[1]["content"]
    assert "original_chars=300000" in first[1]["content"]
    assert first[1]["tool_call_id"] == "call-1"


def test_routed_review_tool_schemas_are_reduced_to_runtime_whitelist():
    tools = [
        {"type": "function", "function": {"name": "memory"}},
        {"type": "function", "function": {"name": "skill_manage"}},
        {"type": "function", "function": {"name": "terminal"}},
    ]

    narrowed = background_review._review_tools_for_request(
        tools,
        whitelist={"memory", "skill_manage"},
        routed=True,
    )

    assert [tool["function"]["name"] for tool in narrowed] == [
        "memory",
        "skill_manage",
    ]
    assert tools[-1]["function"]["name"] == "terminal"


def test_same_model_review_never_advertises_runtime_denied_tool_schema():
    tools = [
        {"type": "function", "function": {"name": "memory"}},
        {"type": "function", "function": {"name": "terminal"}},
    ]

    narrowed = background_review._review_tools_for_request(
        tools,
        whitelist={"memory"},
        routed=False,
    )

    assert [tool["function"]["name"] for tool in narrowed] == ["memory"]


def test_same_model_review_preserves_schema_identity_when_every_tool_is_executable():
    tools = [{"type": "function", "function": {"name": "memory"}}]

    assert background_review._review_tools_for_request(
        tools,
        whitelist={"memory"},
        routed=False,
    ) is tools


def test_context_resolution_uses_smallest_positive_runtime_limit(monkeypatch):
    monkeypatch.setattr(
        background_review,
        "get_model_context_length",
        lambda *_args, **_kwargs: 262_144,
    )
    review_agent = SimpleNamespace(
        model="model",
        provider="nous",
        context_compressor=SimpleNamespace(context_length=1_048_576),
    )

    resolved = background_review._resolve_review_context_length(
        review_agent,
        {
            "base_url": "https://example.invalid/v1",
            "api_key": "redacted",
            "provider": "nous",
        },
    )

    assert resolved == 262_144


def test_over_budget_review_never_calls_provider_or_parent_compression(monkeypatch):
    import model_tools
    import run_agent

    calls = {"provider": 0, "close": 0, "failures": []}

    class FakeReviewAgent:
        def __init__(self, **_kwargs):
            self.model = "incident-model"
            self.provider = "nous"
            self.max_tokens = 8_192
            self.context_compressor = SimpleNamespace(context_length=262_144)
            self.tools = [
                {"type": "function", "function": {"name": "memory"}},
                {"type": "function", "function": {"name": "terminal"}},
            ]
            self._cached_system_prompt = None
            self._session_messages = []

        def run_conversation(self, **_kwargs):
            calls["provider"] += 1

        def shutdown_memory_provider(self):
            pass

        def close(self):
            calls["close"] += 1

    parent_compressor = SimpleNamespace(marker="must-not-change")
    parent = SimpleNamespace(
        provider="nous",
        model="incident-model",
        platform="webui",
        session_id="session-incident",
        _credential_pool=None,
        request_overrides={},
        max_tokens=8_192,
        acp_command=None,
        acp_args=[],
        _memory_store=object(),
        _memory_enabled=True,
        _user_profile_enabled=False,
        _cached_system_prompt="parent prompt",
        session_start=object(),
        memory_notifications="on",
        background_review_callback=None,
        context_compressor=parent_compressor,
        _safe_print=lambda *_args, **_kwargs: None,
        _emit_auxiliary_failure=lambda name, error: calls["failures"].append(
            (name, str(error))
        ),
    )

    monkeypatch.setattr(run_agent, "AIAgent", FakeReviewAgent)
    monkeypatch.setattr(
        background_review,
        "_resolve_review_runtime",
        lambda _agent: {
            "provider": "nous",
            "model": "incident-model",
            "api_key": "redacted",
            "base_url": "https://example.invalid/v1",
            "api_mode": "chat_completions",
            "credential_pool": None,
            "request_overrides": {},
            "max_tokens": 8_192,
            "command": None,
            "args": [],
            "routed": False,
        },
    )
    monkeypatch.setattr(
        background_review,
        "_resolve_review_context_length",
        lambda *_args, **_kwargs: 262_144,
    )
    monkeypatch.setattr(
        background_review,
        "estimate_request_tokens_rough",
        lambda *_args, **_kwargs: 661_596,
    )
    monkeypatch.setattr(
        model_tools,
        "get_tool_definitions",
        lambda **_kwargs: [
            {"type": "function", "function": {"name": "memory"}}
        ],
    )

    background_review._run_review_in_thread(
        parent,
        [{"role": "user", "content": "large captured history"}],
        "review",
    )

    assert calls["provider"] == 0
    assert calls["close"] == 1
    assert calls["failures"] == [
        (
            "background review",
            "aggregate_context_budget_exceeded: requested 708,110 tokens for "
            "262,144-token context",
        )
    ]
    assert parent.context_compressor is parent_compressor
    assert parent.context_compressor.marker == "must-not-change"
    assert parent._last_background_review_admission["admitted"] is False
