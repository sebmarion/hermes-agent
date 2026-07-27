from types import SimpleNamespace

import pytest

from hermes_cli.model_switch import ModelSwitchResult


def _bound(fn, instance):
    return fn.__get__(instance, type(instance))


@pytest.mark.parametrize(
    ("command", "expected_persist_global"),
    [
        ("/model", False),
        ("/model --session", False),
        ("/model --global", True),
        ("/model --global --session", False),
    ],
)
def test_prompt_toolkit_model_picker_preserves_command_persistence_intent(
    monkeypatch, command, expected_persist_global
):
    import cli as cli_mod

    result = ModelSwitchResult(
        success=True,
        new_model="openai/gpt-5.5",
        target_provider="openrouter",
        provider_changed=True,
    )
    switch_calls = []
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **kwargs: switch_calls.append(kwargs) or result,
    )

    picker_context = SimpleNamespace(
        user_providers=None,
        custom_providers=None,
    )
    picker_context.with_overrides = lambda **_kwargs: picker_context
    monkeypatch.setattr(
        "hermes_cli.inventory.load_picker_context",
        lambda: picker_context,
    )
    monkeypatch.setattr(
        "hermes_cli.inventory.build_models_payload",
        lambda _ctx: {
            "providers": [
                {
                    "slug": "openrouter",
                    "is_current": True,
                    "models": ["openai/gpt-5.5"],
                }
            ]
        },
    )
    monkeypatch.setattr(
        "hermes_cli.providers.get_label",
        lambda provider: provider,
    )

    applied = []
    self_ = SimpleNamespace(
        agent=None,
        provider="openrouter",
        model="openai/gpt-5.4",
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-test",
        _app=None,
        _capture_modal_input_snapshot=lambda: None,
        _restore_modal_input_snapshot=lambda: None,
        _invalidate=lambda **_kwargs: None,
    )
    self_._open_model_picker = _bound(cli_mod.HermesCLI._open_model_picker, self_)
    self_._close_model_picker = _bound(cli_mod.HermesCLI._close_model_picker, self_)
    self_._confirm_and_apply_model_switch_result = (
        lambda switch_result, persist_global: applied.append(
            (switch_result, persist_global)
        )
    )

    _bound(cli_mod.HermesCLI._handle_model_switch, self_)(command)

    selection = _bound(cli_mod.HermesCLI._handle_model_picker_selection, self_)
    selection()
    selection()

    assert switch_calls[0]["is_global"] is expected_persist_global
    assert applied == [(result, expected_persist_global)]


def test_prompt_toolkit_model_picker_defers_confirmation_off_key_handler(monkeypatch):
    import cli as cli_mod

    result = ModelSwitchResult(
        success=True,
        new_model="openai/gpt-5.5-pro",
        target_provider="nous",
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **_kwargs: result,
    )

    captured = {}

    class _Thread:
        def __init__(self, *, target, args, daemon):
            captured["target"] = target
            captured["args"] = args
            captured["daemon"] = daemon

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(cli_mod.threading, "Thread", _Thread)

    self_ = SimpleNamespace(
        _app=object(),
        _model_picker_state={
            "stage": "model",
            "provider_data": {"slug": "nous"},
            "model_list": ["openai/gpt-5.5-pro"],
            "selected": 0,
            "user_provs": None,
            "custom_provs": None,
        },
        provider="nous",
        model="openai/gpt-5.5",
        base_url="",
        api_key="",
        _restore_modal_input_snapshot=lambda: None,
        _invalidate=lambda **_kwargs: None,
    )
    self_._close_model_picker = _bound(cli_mod.HermesCLI._close_model_picker, self_)
    self_._confirm_and_apply_model_switch_result = (
        lambda *_args: captured.setdefault("ran_inline", True)
    )

    # An explicit global picker selection keeps confirmation off the key
    # handler while preserving persistence intent.
    _bound(cli_mod.HermesCLI._handle_model_picker_selection, self_)(persist_global=True)

    assert self_._model_picker_state is None
    assert captured["started"] is True
    assert captured["daemon"] is True
    assert captured["args"] == (result, True)
    assert "ran_inline" not in captured


def test_typed_global_switch_warns_when_atomic_route_save_fails(monkeypatch):
    import cli as cli_mod
    import hermes_cli.config as config_mod

    result = ModelSwitchResult(
        success=True,
        new_model="gpt-5.4",
        target_provider="openai-codex",
        provider_changed=True,
        api_key="resolved-runtime-secret",
        base_url="",
        api_mode="codex_responses",
        provider_label="ChatGPT Codex",
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **_kwargs: result,
    )
    picker_context = SimpleNamespace(
        user_providers=None,
        custom_providers=None,
    )
    picker_context.with_overrides = lambda **_kwargs: picker_context
    monkeypatch.setattr(
        "hermes_cli.inventory.load_picker_context",
        lambda: picker_context,
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.resolve_display_context_length",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        config_mod,
        "persist_main_model_assignment",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("read-only filesystem")),
        raising=False,
    )

    lines = []
    legacy_calls = []
    monkeypatch.setattr(cli_mod, "_cprint", lambda value, *a, **k: lines.append(str(value)))
    monkeypatch.setattr(
        cli_mod,
        "save_config_value",
        lambda *args, **kwargs: legacy_calls.append((args, kwargs)),
    )

    self_ = SimpleNamespace(
        agent=None,
        conversation_history=[],
        provider="custom",
        requested_provider="custom",
        model="old-model",
        base_url="https://old.example/v1",
        api_key="old-secret",
        api_mode="chat_completions",
        _explicit_api_key="old-secret",
        _explicit_base_url="https://old.example/v1",
        _confirm_expensive_model_switch=lambda _result: True,
        _pending_model_switch_note="",
    )

    _bound(cli_mod.HermesCLI._handle_model_switch, self_)(
        "/model gpt-5.4 --provider openai-codex --global"
    )

    assert self_.model == "gpt-5.4"
    assert legacy_calls == []
    assert any("Model switched" in line for line in lines)
    assert any(
        "not saved" in line.lower() and "global" in line.lower() for line in lines
    )
    assert not any("Saved to config.yaml" in line for line in lines)
