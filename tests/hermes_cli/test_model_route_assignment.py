"""Behavior tests for shared main-model route assignment."""

import pytest

from hermes_cli import config


@pytest.mark.parametrize("current", [None, "legacy-model"])
def test_assignment_coerces_scalar_config_and_sets_route_together(current):
    result = config.apply_main_model_assignment(
        current,
        provider="openrouter",
        model="anthropic/claude-sonnet-4",
    )

    assert result == {
        "provider": "openrouter",
        "default": "anthropic/claude-sonnet-4",
    }


def test_provider_change_drops_endpoint_fields_not_owned_by_target():
    result = config.apply_main_model_assignment(
        {
            "provider": "custom",
            "default": "old-model",
            "base_url": "https://old.example/v1",
            "api_mode": "anthropic_messages",
            "api_key": "old-secret",
            "api": "legacy-secret",
            "keep": {"unrelated": True},
        },
        provider="openrouter",
        model="new-model",
    )

    assert result == {
        "provider": "openrouter",
        "default": "new-model",
        "keep": {"unrelated": True},
    }


def test_provider_change_applies_explicit_target_endpoint_fields():
    result = config.apply_main_model_assignment(
        {
            "provider": "custom",
            "base_url": "https://old.example/v1",
            "api_mode": "anthropic_messages",
            "api_key": "old-secret",
            "api": "legacy-secret",
        },
        provider="local",
        model="new-model",
        base_url=" https://new.example/v1 ",
        api_key=" new-secret ",
        api_mode=" chat_completions ",
    )

    assert result == {
        "provider": "local",
        "default": "new-model",
        "base_url": "https://new.example/v1",
        "api_key": "new-secret",
        "api_mode": "chat_completions",
    }


def test_same_provider_preserves_endpoint_fields_not_explicitly_replaced():
    result = config.apply_main_model_assignment(
        {
            "provider": "custom",
            "default": "old-model",
            "base_url": "https://same.example/v1",
            "api_mode": "anthropic_messages",
            "api_key": "same-secret",
            "api": "legacy-secret",
        },
        provider="custom",
        model="new-model",
    )

    assert result == {
        "provider": "custom",
        "default": "new-model",
        "base_url": "https://same.example/v1",
        "api_mode": "anthropic_messages",
        "api_key": "same-secret",
        "api": "legacy-secret",
    }


def test_same_provider_url_change_drops_old_endpoint_credentials():
    result = config.apply_main_model_assignment(
        {
            "provider": "custom",
            "base_url": "https://old.example/v1",
            "api_mode": "anthropic_messages",
            "api_key": "old-secret",
            "api": "legacy-secret",
        },
        provider="custom",
        model="new-model",
        base_url="https://new.example/v1",
    )

    assert result == {
        "provider": "custom",
        "default": "new-model",
        "base_url": "https://new.example/v1",
    }


def test_explicit_endpoint_fields_replace_same_provider_values_and_key_alias():
    result = config.apply_main_model_assignment(
        {
            "provider": "custom",
            "base_url": "https://old.example/v1",
            "api_mode": "anthropic_messages",
            "api_key": "old-secret",
            "api": "legacy-secret",
        },
        provider="custom",
        model="new-model",
        base_url="https://new.example/v1",
        api_key="new-secret",
        api_mode="chat_completions",
    )

    assert result["base_url"] == "https://new.example/v1"
    assert result["api_key"] == "new-secret"
    assert result["api_mode"] == "chat_completions"
    assert "api" not in result


def test_explicit_same_normalized_url_preserves_endpoint_credentials():
    result = config.apply_main_model_assignment(
        {
            "provider": "custom",
            "base_url": "https://same.example/v1",
            "api_mode": "anthropic_messages",
            "api_key": "same-secret",
        },
        provider="custom",
        model="new-model",
        base_url="  https://same.example/v1  ",
    )

    assert result["base_url"] == "https://same.example/v1"
    assert result["api_mode"] == "anthropic_messages"
    assert result["api_key"] == "same-secret"


def test_assignment_strips_provider_and_model_values():
    result = config.apply_main_model_assignment(
        None,
        provider="  custom  ",
        model="  local-model  ",
        base_url="  https://endpoint.example/v1  ",
    )

    assert result == {
        "provider": "custom",
        "default": "local-model",
        "base_url": "https://endpoint.example/v1",
    }


def test_assignment_always_drops_model_context_and_preserves_other_siblings():
    current = {
        "provider": "openrouter",
        "default": "old-model",
        "context_length": 8192,
        "temperature": 0.2,
        "fallbacks": ["backup-model"],
    }

    result = config.apply_main_model_assignment(
        current,
        provider="openrouter",
        model="new-model",
    )

    assert result is current
    assert "context_length" not in result
    assert result["temperature"] == 0.2
    assert result["fallbacks"] == ["backup-model"]
