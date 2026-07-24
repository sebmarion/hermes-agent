from types import SimpleNamespace

from hermes_cli.subcommands.bestplan import cmd_bestplan


def _canonical_config():
    return {
        "enabled": True,
        "explorers": [
            {
                "name": "glm",
                "provider": "custom:neuralwatt",
                "model": "glm-5.2",
                "api_mode": "chat_completions",
                "reasoning_effort": "high",
            },
            {
                "name": "kimi-k3",
                "provider": "kimi-coding",
                "model": "k3",
                "api_mode": "anthropic_messages",
                "reasoning_effort": "max",
            },
            {
                "name": "sol",
                "provider": "openai-codex",
                "model": "gpt-5.6-sol",
                "api_mode": "codex_app_server",
                "reasoning_effort": "ultra",
            },
        ],
        "synthesizer": "sol",
        "explorer_timeout": 180,
        "synthesizer_timeout": 180,
        "overall_timeout": 540,
    }


def test_lanes_prints_canonical_explorers_in_order(monkeypatch, capsys):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"bestplan": _canonical_config()},
    )

    assert cmd_bestplan(SimpleNamespace(bestplan_command="lanes")) == 0
    output = capsys.readouterr().out

    assert output.index("glm") < output.index("kimi-k3") < output.index("sol")
    assert "kimi-coding" in output
    assert "k3" in output
    assert "Synthesizer: sol" in output
    assert "Validation: PASS" in output


def test_lanes_renders_legacy_config_through_canonical_view(monkeypatch, capsys):
    canonical = _canonical_config()
    legacy = {
        **{key: value for key, value in canonical.items() if key != "explorers"},
        "lanes": canonical["explorers"],
    }
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"bestplan": legacy},
    )

    assert cmd_bestplan(SimpleNamespace(bestplan_command="lanes")) == 0
    output = capsys.readouterr().out

    assert "BestPlan SOTA Explorers" in output
    assert output.index("glm") < output.index("kimi-k3") < output.index("sol")
    assert "Synthesizer: sol" in output


def test_invalid_config_output_is_sanitized(monkeypatch, capsys):
    sentinel = "SENTINEL_SECRET"
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "bestplan": {
                "enabled": True,
                "explorers": [],
                "synthesizer": "sol",
                sentinel: sentinel,
            }
        },
    )

    exit_code = cmd_bestplan(SimpleNamespace(bestplan_command="lanes"))
    output = capsys.readouterr().out

    assert exit_code == 1
    assert sentinel not in output
    assert "Validation: FAIL" in output
    assert "invalid BestPlan configuration" in output
