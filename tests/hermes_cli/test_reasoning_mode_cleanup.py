"""Ordinary reasoning writers must leave no stale WebUI Ultra mode."""

import pytest


@pytest.mark.parametrize(
    "helper_path",
    [
        "hermes_cli.main._set_reasoning_effort",
        "hermes_cli.setup._set_reasoning_effort",
    ],
)
def test_reasoning_effort_helpers_clear_stale_ultra_mode(helper_path):
    module_name, helper_name = helper_path.rsplit(".", 1)
    module = __import__(module_name, fromlist=[helper_name])
    helper = getattr(module, helper_name)
    config = {
        "agent": {
            "reasoning_effort": "max",
            "reasoning_mode": "ultra",
            "max_turns": 90,
        }
    }

    helper(config, "high")

    assert config["agent"]["reasoning_effort"] == "high"
    assert "reasoning_mode" not in config["agent"]
    assert config["agent"]["max_turns"] == 90
