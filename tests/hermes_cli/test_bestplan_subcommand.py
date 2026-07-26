from types import SimpleNamespace

from hermes_cli.subcommands.bestplan import cmd_bestplan


def test_lanes_command_renders_legacy_keyed_lane_mapping(monkeypatch, capsys):
    keyed_lanes = {
        "glm": {
            "provider": "neuralwatt",
            "model": "glm-5.2",
            "api_mode": "chat_completions",
            "reasoning_effort": "high",
        },
        "sol": {
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
            "api_mode": "codex_app_server",
            "reasoning_effort": "ultra",
        },
    }
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"bestplan": {"lanes": keyed_lanes}},
    )

    status = cmd_bestplan(SimpleNamespace(bestplan_command="lanes"))

    output = capsys.readouterr().out
    assert status == 0
    assert "glm" in output
    assert "sol" in output
    assert "Validation: PASS" in output
