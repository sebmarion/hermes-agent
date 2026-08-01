"""Tests for content-free provider-request admission receipts."""

from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest

from agent.chat_completion_helpers import (
    build_provider_request_admission_receipt,
    estimate_request_context_tokens,
)


def _agent(
    *,
    context_length: int = 100_000,
    threshold_tokens: int = 80_000,
    model: str = "gpt-5",
    provider: str = "openai",
    compressor_model: str | None = None,
    compressor_provider: str | None = None,
):
    return SimpleNamespace(
        model=model,
        provider=provider,
        context_compressor=SimpleNamespace(
            model=model if compressor_model is None else compressor_model,
            provider=provider if compressor_provider is None else compressor_provider,
            context_length=context_length,
            threshold_tokens=threshold_tokens,
        ),
    )


def test_chat_completions_uses_minimum_margin_without_payload_content():
    api_kwargs = {
        "model": "gpt-5",
        "messages": [{"role": "user", "content": "PRIVATE_MESSAGE_" * 100}],
        "tools": [{"type": "function", "name": "PRIVATE_TOOL"}],
    }
    estimated = estimate_request_context_tokens(api_kwargs)
    threshold = estimated + 1_024

    receipt = build_provider_request_admission_receipt(
        _agent(context_length=threshold + 5_000, threshold_tokens=threshold),
        api_kwargs,
    )

    assert receipt["estimated_input_tokens"] == estimated
    assert receipt["margin_tokens"] == 1_024
    assert receipt["effective_input_ceiling"] == threshold
    assert receipt["category_estimated_tokens"]["messages"] > 0
    assert receipt["category_estimated_tokens"]["input"] == 0
    assert receipt["category_estimated_tokens"]["instructions"] == 0
    assert receipt["category_estimated_tokens"]["tools"] > 0
    assert receipt["category_estimated_tokens"]["total"] == estimated
    assert receipt["decision"] == "admit"
    serialized = json.dumps(receipt)
    assert "PRIVATE_MESSAGE" not in serialized
    assert "PRIVATE_TOOL" not in serialized


def test_responses_uses_five_percent_margin_and_rejects_over_ceiling():
    api_kwargs = {
        "model": "gpt-5",
        "input": "PRIVATE_INPUT_" * 8_000,
        "instructions": "PRIVATE_INSTRUCTIONS_" * 100,
        "tools": [{"name": "PRIVATE_RESPONSES_TOOL", "description": "x" * 500}],
    }
    estimated = estimate_request_context_tokens(api_kwargs)
    margin = math.ceil(estimated * 0.05)
    assert margin > 1_024

    receipt = build_provider_request_admission_receipt(
        _agent(
            context_length=estimated + margin + 10_000,
            threshold_tokens=estimated + margin - 1,
        ),
        api_kwargs,
    )

    assert receipt["estimated_input_tokens"] == estimated
    assert receipt["margin_tokens"] == margin
    assert receipt["category_estimated_tokens"]["input"] > 0
    assert receipt["category_estimated_tokens"]["instructions"] > 0
    assert receipt["category_estimated_tokens"]["tools"] > 0
    assert receipt["category_estimated_tokens"]["total"] == estimated
    assert receipt["decision"] == "reject"
    assert receipt["reason"] == "estimated_input_plus_margin_exceeds_ceiling"
    serialized = json.dumps(receipt)
    assert "PRIVATE_INPUT" not in serialized
    assert "PRIVATE_INSTRUCTIONS" not in serialized
    assert "PRIVATE_RESPONSES_TOOL" not in serialized


@pytest.mark.parametrize(
    "output_key",
    ["max_tokens", "max_completion_tokens", "max_output_tokens"],
)
def test_explicit_provider_output_reserve_is_read_from_each_supported_key(output_key):
    api_kwargs = {"input": "x" * 400, output_key: 2_500}

    receipt = build_provider_request_admission_receipt(
        _agent(context_length=10_000, threshold_tokens=9_000),
        api_kwargs,
    )

    assert receipt["explicit_output_tokens"] == 2_500
    assert receipt["window_input_ceiling"] == 7_500
    assert receipt["effective_input_ceiling"] == 7_500


@pytest.mark.parametrize(
    ("context_length", "threshold_tokens", "output_tokens", "expected_ceiling"),
    [
        (12_000, 7_000, 2_000, 7_000),
        (10_000, 9_000, 2_500, 7_500),
    ],
)
def test_effective_ceiling_is_smaller_of_threshold_and_reserved_window(
    context_length,
    threshold_tokens,
    output_tokens,
    expected_ceiling,
):
    receipt = build_provider_request_admission_receipt(
        _agent(context_length=context_length, threshold_tokens=threshold_tokens),
        {"messages": [], "max_tokens": output_tokens},
    )

    assert receipt["effective_input_ceiling"] == expected_ceiling
    assert receipt["decision"] == "admit"


def test_absent_output_cap_still_uses_context_window_as_a_ceiling():
    api_kwargs = {"input": "x" * 4_000}
    estimated = estimate_request_context_tokens(api_kwargs)
    required_tokens = estimated + max(1_024, math.ceil(estimated * 0.05))

    receipt = build_provider_request_admission_receipt(
        _agent(
            context_length=required_tokens - 1,
            threshold_tokens=required_tokens + 1_000,
        ),
        api_kwargs,
    )

    assert receipt["explicit_output_tokens"] == 0
    assert receipt["window_input_ceiling"] == required_tokens - 1
    assert receipt["effective_input_ceiling"] == required_tokens - 1
    assert receipt["decision"] == "reject"


@pytest.mark.parametrize("raw_output", [0, -1, False, "2048", 2_048.0])
def test_present_non_positive_or_non_integer_output_limits_fail_closed(raw_output):
    receipt = build_provider_request_admission_receipt(
        _agent(context_length=6_000, threshold_tokens=5_000),
        {"input": "x" * 400, "max_output_tokens": raw_output},
    )

    assert receipt["explicit_output_tokens"] == 0
    assert receipt["effective_input_ceiling"] == 5_000
    assert receipt["decision"] == "reject"
    assert receipt["reason"] == "invalid_explicit_output_tokens"


def test_multiple_valid_output_caps_reserve_the_largest_value():
    receipt = build_provider_request_admission_receipt(
        _agent(context_length=10_000, threshold_tokens=9_000),
        {
            "input": "x" * 400,
            "max_tokens": 4_000,
            "max_completion_tokens": 2_000,
            "max_output_tokens": 1_000,
        },
    )

    assert receipt["explicit_output_tokens"] == 4_000
    assert receipt["window_input_ceiling"] == 6_000
    assert receipt["effective_input_ceiling"] == 6_000


def test_valid_output_cap_cannot_mask_another_invalid_present_cap():
    receipt = build_provider_request_admission_receipt(
        _agent(context_length=10_000, threshold_tokens=9_000),
        {
            "input": "x" * 400,
            "max_output_tokens": 1_000,
            "max_tokens": 0,
        },
    )

    assert receipt["decision"] == "reject"
    assert receipt["reason"] == "invalid_explicit_output_tokens"


@pytest.mark.parametrize(
    ("context_length", "threshold_tokens", "reason"),
    [
        (0, 5_000, "invalid_compressor_context_length"),
        (-1, 5_000, "invalid_compressor_context_length"),
        (6_000, 0, "invalid_compressor_threshold_tokens"),
        (6_000, -1, "invalid_compressor_threshold_tokens"),
    ],
)
def test_non_positive_compressor_limits_fail_closed(
    context_length,
    threshold_tokens,
    reason,
):
    receipt = build_provider_request_admission_receipt(
        _agent(context_length=context_length, threshold_tokens=threshold_tokens),
        {"input": "small"},
    )

    assert receipt["decision"] == "reject"
    assert receipt["reason"] == reason


@pytest.mark.parametrize("output_tokens", [6_000, 6_001])
def test_non_positive_reserved_window_ceiling_fails_closed(output_tokens):
    receipt = build_provider_request_admission_receipt(
        _agent(context_length=6_000, threshold_tokens=5_000),
        {"input": "small", "max_output_tokens": output_tokens},
    )

    assert receipt["window_input_ceiling"] <= 0
    assert receipt["decision"] == "reject"
    assert receipt["reason"] == "non_positive_window_input_ceiling"


@pytest.mark.parametrize(
    ("compressor_model", "compressor_provider", "reason"),
    [
        ("gpt-4", "openai", "compressor_model_mismatch"),
        ("gpt-5", "anthropic", "compressor_provider_mismatch"),
    ],
)
def test_compressor_identity_mismatch_fails_closed(
    compressor_model,
    compressor_provider,
    reason,
):
    receipt = build_provider_request_admission_receipt(
        _agent(
            compressor_model=compressor_model,
            compressor_provider=compressor_provider,
        ),
        {"input": "small"},
    )

    assert receipt["decision"] == "reject"
    assert receipt["reason"] == reason


def test_identity_comparison_trims_and_ignores_case():
    receipt = build_provider_request_admission_receipt(
        _agent(
            model=" GPT-5 ",
            provider=" OpenAI ",
            compressor_model="gpt-5",
            compressor_provider="openai",
        ),
        {"input": "small"},
    )

    assert receipt["resolved_model"] == "GPT-5"
    assert receipt["resolved_provider"] == "OpenAI"
    assert receipt["decision"] == "admit"
