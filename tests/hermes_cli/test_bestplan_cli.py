from types import SimpleNamespace

from hermes_cli.subcommands.bestplan import cmd_bestplan


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
