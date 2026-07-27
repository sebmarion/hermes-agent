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
