"""Regression test for the `/model` picker confirmation display.

Bug (April 2026): after choosing a model from the interactive `/model` picker,
``HermesCLI._apply_model_switch_result()`` printed ``ModelInfo.context_window``
straight from models.dev, which always reports the vendor-wide value (e.g.
gpt-5.5 = 1,050,000 on ``openai``). That ignored provider-specific caps — in
particular, ChatGPT Codex OAuth enforces 272K on the same slug. The sibling
``_handle_model_switch()`` (typed ``/model <name>``) was already fixed to use
``resolve_display_context_length()``; the picker path was missed, causing
"sometimes 1M, sometimes 272K" for the same model across sibling UI paths.

Fix: both display paths now go through ``resolve_display_context_length()``.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from hermes_cli.model_switch import ModelSwitchResult


class _FakeModelInfo:
    context_window = 1_050_000
    max_output = 0

    def has_cost_data(self):
        return False

    def format_capabilities(self):
        return ""


class _StubCLI:
    """Minimum attrs ``_apply_model_switch_result`` reads on ``self``."""
    agent = None
    model = ""
    provider = ""
    requested_provider = ""
    api_key = ""
    _explicit_api_key = ""
    base_url = ""
    _explicit_base_url = ""
    api_mode = ""
    _pending_model_switch_note = ""
    conversation_history = []


def _run_display(monkeypatch, result):
    import cli as cli_mod

    captured: list[str] = []
    monkeypatch.setattr(cli_mod, "_cprint", lambda s, *a, **k: captured.append(str(s)))
    # Avoid writing to ~/.hermes/config.yaml during the test.
    monkeypatch.setattr(cli_mod, "save_config_value", lambda *a, **k: None)
    cli_mod.HermesCLI._apply_model_switch_result(_StubCLI(), result, False)
    return captured


def test_picker_path_uses_provider_aware_context_on_codex(monkeypatch):
    """``_apply_model_switch_result`` must prefer the provider-aware resolver
    (272K on Codex) over the raw models.dev value (1.05M for gpt-5.5).
    """
    result = ModelSwitchResult(
        success=True,
        new_model="gpt-5.5",
        target_provider="openai-codex",
        provider_changed=True,
        api_key="",
        base_url="https://chatgpt.com/backend-api/codex",
        api_mode="codex_responses",
        warning_message="",
        provider_label="ChatGPT Codex",
        resolved_via_alias=False,
        capabilities=None,
        model_info=_FakeModelInfo(),  # models.dev says 1.05M
        is_global=False,
    )
    with patch(
        "agent.model_metadata.get_model_context_length",
        return_value=272_000,
    ):
        lines = _run_display(monkeypatch, result)

    ctx_line = next((l for l in lines if "Context:" in l), "")
    assert "272,000" in ctx_line, (
        f"picker-path display must show Codex's 272K cap, got: {ctx_line!r}"
    )
    assert "1,050,000" not in ctx_line, (
        f"picker-path display leaked models.dev's 1.05M for Codex: {ctx_line!r}"
    )


def test_picker_path_falls_back_to_model_info_when_resolver_empty(monkeypatch):
    """If ``get_model_context_length`` returns nothing (rare — truly unknown
    endpoint), the display still surfaces ``ModelInfo.context_window`` so the
    user sees *something* rather than a silent blank.
    """
    result = ModelSwitchResult(
        success=True,
        new_model="some-model",
        target_provider="some-provider",
        provider_changed=True,
        api_key="",
        base_url="",
        api_mode="chat_completions",
        warning_message="",
        provider_label="Some Provider",
        resolved_via_alias=False,
        capabilities=None,
        model_info=_FakeModelInfo(),  # context_window = 1_050_000
        is_global=False,
    )
    with patch(
        "agent.model_metadata.get_model_context_length",
        return_value=None,
    ):
        lines = _run_display(monkeypatch, result)

    ctx_line = next((l for l in lines if "Context:" in l), "")
    assert "1,050,000" in ctx_line, (
        f"resolver-empty path should fall back to ModelInfo, got: {ctx_line!r}"
    )


def test_global_switch_clears_context_pin_owned_by_previous_route(monkeypatch):
    import cli as cli_mod

    writes = []
    monkeypatch.setattr(cli_mod, "_cprint", lambda *_a, **_k: None)
    monkeypatch.setattr(
        cli_mod,
        "save_config_value",
        lambda key, value: writes.append((key, value)),
    )
    cli = _StubCLI()
    cli.model = "shared-model"
    cli.provider = "custom"
    # Runtime may already diverge from persisted config through a session override.
    cli.base_url = "https://small.example/v1"
    result = ModelSwitchResult(
        success=True,
        new_model="shared-model",
        target_provider="custom",
        provider_changed=False,
        api_key="",
        base_url="https://small.example/v1",
        api_mode="chat_completions",
        warning_message="",
        provider_label="Custom",
        resolved_via_alias=False,
        capabilities=None,
        model_info=_FakeModelInfo(),
        is_global=True,
    )

    configured = {
        "model": {
            "default": "shared-model",
            "provider": "custom",
            "base_url": "https://large.example/v1",
            "context_length": 1_048_576,
        }
    }
    with (
        patch(
            "agent.model_metadata.get_model_context_length",
            return_value=256_000,
        ),
        patch("hermes_cli.config.load_config_readonly", return_value=configured),
    ):
        cli_mod.HermesCLI._apply_model_switch_result(cli, result, True)

    assert ("model.context_length", None) in writes

def test_picker_runtime_switch_failure_performs_zero_config_writes(monkeypatch):
    import cli as cli_mod
    import hermes_cli.config as config_mod

    class _FailingAgent:
        _config_context_length = None

        def switch_model(self, **_kwargs):
            raise RuntimeError("client rebuild failed")

    result = ModelSwitchResult(
        success=True,
        new_model="gpt-5.4",
        target_provider="openai-codex",
        provider_changed=True,
        api_key="runtime-secret",
        base_url="",
        api_mode="codex_responses",
        provider_label="ChatGPT Codex",
    )
    cli = _StubCLI()
    cli.agent = _FailingAgent()
    cli.model = "old-model"
    cli.provider = "custom"
    cli.requested_provider = "custom"
    cli.base_url = "https://old.example/v1"
    cli.api_key = "old-secret"
    cli.api_mode = "chat_completions"
    cli._explicit_api_key = "old-secret"
    cli._explicit_base_url = "https://old.example/v1"
    calls = []
    lines = []
    monkeypatch.setattr(cli_mod, "_cprint", lambda value, *a, **k: lines.append(str(value)))
    monkeypatch.setattr(
        "hermes_cli.context_switch_guard.merge_preflight_compression_warning",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        config_mod,
        "persist_main_model_assignment",
        lambda **kwargs: calls.append(kwargs),
        raising=False,
    )
    monkeypatch.setattr(
        cli_mod,
        "save_config_value",
        lambda *a, **k: pytest.fail("legacy dotted writes must not run"),
    )

    cli_mod.HermesCLI._apply_model_switch_result(cli, result, True)

    assert calls == []
    assert cli.model == "old-model"
    assert any("failed" in line and "staying on old-model" in line for line in lines)


def test_picker_persistence_failure_keeps_session_switch_and_warns_unsaved(
    monkeypatch
):
    import cli as cli_mod
    import hermes_cli.config as config_mod

    result = ModelSwitchResult(
        success=True,
        new_model="gpt-5.4",
        target_provider="openai-codex",
        provider_changed=True,
        api_key="runtime-secret",
        base_url="",
        api_mode="codex_responses",
        provider_label="ChatGPT Codex",
    )
    cli = _StubCLI()
    cli.agent = None
    cli.model = "old-model"
    cli.provider = "custom"
    cli.requested_provider = "custom"
    cli.base_url = "https://old.example/v1"
    cli.api_key = "old-secret"
    cli.api_mode = "chat_completions"
    cli._explicit_api_key = "old-secret"
    cli._explicit_base_url = "https://old.example/v1"
    lines = []
    legacy_calls = []
    monkeypatch.setattr(cli_mod, "_cprint", lambda value, *a, **k: lines.append(str(value)))
    monkeypatch.setattr(
        "hermes_cli.model_switch.resolve_display_context_length",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        config_mod,
        "persist_main_model_assignment",
        lambda **kwargs: (_ for _ in ()).throw(OSError("disk full")),
        raising=False,
    )
    monkeypatch.setattr(
        cli_mod,
        "save_config_value",
        lambda *args, **kwargs: legacy_calls.append((args, kwargs)),
    )

    cli_mod.HermesCLI._apply_model_switch_result(cli, result, True)

    assert cli.model == "gpt-5.4"
    assert legacy_calls == []
    assert any("Model switched" in line for line in lines)
    assert any(
        "not saved" in line.lower() and "global" in line.lower() for line in lines
    )
    assert not any("Saved to config.yaml" in line for line in lines)
