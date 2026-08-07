from types import SimpleNamespace

from hermes_cli.subcommands.bestplan import cmd_bestplan


def _canonical_config():
    return {
        "enabled": True,
        "explorers": [
            {
                "name": "alpha",
                "provider": "provider-alpha",
                "model": "model-alpha",
                "api_mode": "chat_completions",
                "reasoning_effort": "high",
            },
            {
                "name": "sol",
                "provider": "openai-codex",
                "model": "model-sol",
                "api_mode": "codex_app_server",
                "reasoning_effort": "ultra",
            },
        ],
        "synthesizer": "sol",
        "explorer_timeout": 180,
        "synthesizer_timeout": 180,
        "overall_timeout": 540,
    }


def test_cli_prints_canonical_explorers_and_named_synthesizer(monkeypatch, capsys):
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: {"bestplan": _canonical_config()}
    )

    assert cmd_bestplan(SimpleNamespace(bestplan_command="lanes")) == 0
    output = capsys.readouterr().out
    assert "BestPlan SOTA Explorers" in output
    assert output.index("alpha") < output.index("sol")
    assert "Synthesizer: sol" in output
    assert "Validation: PASS" in output


def test_cli_invalid_config_error_is_sanitized(monkeypatch, capsys):
    sentinel = "SENTINEL_SECRET"
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "bestplan": {
                "explorers": [],
                "synthesizer": "sol",
                sentinel: sentinel,
            }
        },
    )

    assert cmd_bestplan(SimpleNamespace(bestplan_command="lanes")) == 1
    output = capsys.readouterr().out
    assert sentinel not in output
    assert "Validation: FAIL" in output
    assert "invalid BestPlan configuration" in output
