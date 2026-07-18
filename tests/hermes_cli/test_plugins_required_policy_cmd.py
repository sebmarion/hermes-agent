"""Required-policy configuration and CLI ownership tests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml


STATUS_FIELDS = {
    "plugin",
    "policy",
    "configured",
    "installed",
    "enabled",
    "loaded",
    "registered",
    "quarantined",
    "timeout_ms",
    "last_error_code",
}


def _write_config(home: Path, config: object) -> None:
    (home / "config.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )


def _read_config(home: Path) -> dict:
    return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}


def _make_plugin(
    home: Path,
    key: str,
    *,
    policies: object = None,
    register_timeout_ms: int | None = None,
    init_source: str | None = None,
) -> Path:
    plugin_dir = home / "plugins" / key
    plugin_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": key.split("/")[-1],
        "version": "1.0.0",
        "policies": [] if policies is None else policies,
    }
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )
    if init_source is not None:
        source = init_source
    elif register_timeout_ms is not None:
        source = (
            "def register(ctx):\n"
            "    def policy(payload):\n"
            "        return {'action': 'allow', "
            "'policy_binding': payload['policy_binding']}\n"
            f"    ctx.register_policy('tool_dispatch', policy, "
            f"timeout_ms={register_timeout_ms})\n"
        )
    else:
        source = "def register(ctx):\n    pass\n"
    (plugin_dir / "__init__.py").write_text(source, encoding="utf-8")
    return plugin_dir


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "hermes-home"
    (home / "plugins").mkdir(parents=True)
    _write_config(home, {})
    empty_bundled = tmp_path / "no-bundled-plugins"
    empty_bundled.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_ENABLE_PROJECT_PLUGINS", raising=False)

    import hermes_cli.plugins as plugins_module

    monkeypatch.setattr(plugins_module, "_plugin_manager", None)
    monkeypatch.setattr(
        plugins_module,
        "get_bundled_plugins_dir",
        lambda: empty_bundled,
    )
    monkeypatch.setattr(
        plugins_module.PluginManager,
        "_scan_entry_points",
        lambda self: [],
    )
    return home


def test_get_required_policies_defaults_to_empty(isolated_home: Path) -> None:
    from hermes_cli.plugins_cmd import _get_required_policies

    assert _get_required_policies() == {}


@pytest.mark.parametrize(
    "config",
    [
        {"plugins": "invalid"},
        {"plugins": {"required_policies": "invalid"}},
        {"plugins": {"required_policies": {"": ["tool_dispatch"]}}},
        {"plugins": {"required_policies": {7: ["tool_dispatch"]}}},
        {"plugins": {"required_policies": {"governor": "tool_dispatch"}}},
        {"plugins": {"required_policies": {"governor": [7]}}},
        {"plugins": {"required_policies": {"governor": [""]}}},
        {"plugins": {"required_policies": {"governor": ["unknown"]}}},
    ],
)
def test_get_required_policies_rejects_malformed_config(
    isolated_home: Path,
    config: dict,
) -> None:
    from hermes_cli.plugins_cmd import (
        RequiredPolicyConfigError,
        _get_required_policies,
    )

    _write_config(isolated_home, config)

    with pytest.raises(RequiredPolicyConfigError):
        _get_required_policies()


def test_save_normalizes_and_preserves_foreign_config(isolated_home: Path) -> None:
    from hermes_cli.plugins_cmd import _save_required_policies

    _write_config(
        isolated_home,
        {
            "custom_foreign": {"provider": "zeus"},
            "plugins": {
                "enabled": ["keep-enabled"],
                "entries": {"keep": {"secret": "DO_NOT_REPLACE"}},
            },
        },
    )

    _save_required_policies({
        "z-plugin": ["tool_dispatch", "tool_dispatch"],
        "a-plugin": ["tool_dispatch"],
    })

    config = _read_config(isolated_home)
    assert config["custom_foreign"] == {"provider": "zeus"}
    assert config["plugins"]["enabled"] == ["keep-enabled"]
    assert config["plugins"]["entries"] == {"keep": {"secret": "DO_NOT_REPLACE"}}
    assert list(config["plugins"]["required_policies"]) == [
        "a-plugin",
        "z-plugin",
    ]
    assert config["plugins"]["required_policies"]["z-plugin"] == ["tool_dispatch"]


def test_require_policy_uses_manifest_and_does_not_enable_plugin(
    isolated_home: Path,
) -> None:
    from hermes_cli.plugins_cmd import cmd_require_policy

    _make_plugin(isolated_home, "governor", policies=["tool_dispatch"])
    _write_config(
        isolated_home,
        {"plugins": {"disabled": ["keep-disabled"]}, "foreign": "keep"},
    )

    cmd_require_policy(SimpleNamespace(plugin="governor", policy="tool_dispatch"))
    first = _read_config(isolated_home)
    cmd_require_policy(SimpleNamespace(plugin="governor", policy="tool_dispatch"))
    second = _read_config(isolated_home)

    assert first == second
    assert second["plugins"]["required_policies"] == {"governor": ["tool_dispatch"]}
    assert "enabled" not in second["plugins"]
    assert second["plugins"]["disabled"] == ["keep-disabled"]
    assert second["foreign"] == "keep"


@pytest.mark.parametrize(
    ("plugin", "policies", "requested_policy"),
    [
        ("missing", None, "tool_dispatch"),
        ("governor", [], "tool_dispatch"),
        ("governor", ["unknown"], "tool_dispatch"),
        ("governor", ["tool_dispatch"], "unknown"),
    ],
)
def test_require_policy_rejects_unprovable_requirements(
    isolated_home: Path,
    plugin: str,
    policies: object,
    requested_policy: str,
) -> None:
    from hermes_cli.plugins_cmd import cmd_require_policy

    if policies is not None:
        _make_plugin(isolated_home, plugin, policies=policies)

    with pytest.raises(SystemExit) as exc_info:
        cmd_require_policy(SimpleNamespace(plugin=plugin, policy=requested_policy))

    assert exc_info.value.code == 1
    assert _read_config(isolated_home).get("plugins", {}).get("required_policies") in (
        None,
        {},
    )


def test_unrequire_policy_removes_exact_missing_plugin_pair(
    isolated_home: Path,
) -> None:
    from hermes_cli.plugins_cmd import cmd_unrequire_policy

    original = {
        "foreign": {"keep": True},
        "plugins": {
            "enabled": ["another-plugin"],
            "entries": {"keep": {"token": "DO_NOT_REPLACE"}},
            "required_policies": {
                "gone-plugin": ["tool_dispatch"],
                "other-plugin": ["tool_dispatch"],
            },
        },
    }
    _write_config(isolated_home, original)

    args = SimpleNamespace(plugin="gone-plugin", policy="tool_dispatch")
    cmd_unrequire_policy(args)
    first = _read_config(isolated_home)
    cmd_unrequire_policy(args)
    second = _read_config(isolated_home)

    assert first == second
    assert second["foreign"] == original["foreign"]
    assert second["plugins"]["enabled"] == ["another-plugin"]
    assert second["plugins"]["entries"] == original["plugins"]["entries"]
    assert second["plugins"]["required_policies"] == {"other-plugin": ["tool_dispatch"]}


def test_policy_status_json_reports_disabled_unloaded_plugin(
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from hermes_cli.plugins_cmd import cmd_policy_status

    _make_plugin(isolated_home, "governor", policies=["tool_dispatch"])
    _write_config(
        isolated_home,
        {
            "plugins": {
                "required_policies": {"governor": ["tool_dispatch"]},
            }
        },
    )

    cmd_policy_status(SimpleNamespace(json=True))

    records = json.loads(capsys.readouterr().out)
    assert records == [
        {
            "configured": True,
            "enabled": False,
            "installed": True,
            "last_error_code": None,
            "loaded": False,
            "plugin": "governor",
            "policy": "tool_dispatch",
            "quarantined": False,
            "registered": False,
            "timeout_ms": None,
        }
    ]
    assert set(records[0]) == STATUS_FIELDS


def test_policy_status_json_reports_loaded_registration_and_timeout(
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from hermes_cli.plugins_cmd import cmd_policy_status

    _make_plugin(
        isolated_home,
        "governor",
        policies=["tool_dispatch"],
        register_timeout_ms=3750,
    )
    _write_config(
        isolated_home,
        {
            "plugins": {
                "enabled": ["governor"],
                "required_policies": {"governor": ["tool_dispatch"]},
                "entries": {"governor": {"secret": "TOP_SECRET_VALUE"}},
            }
        },
    )

    cmd_policy_status(SimpleNamespace(json=True))

    output = capsys.readouterr().out
    records = json.loads(output)
    assert len(records) == 1
    assert set(records[0]) == STATUS_FIELDS
    assert records[0] == {
        "configured": True,
        "enabled": True,
        "installed": True,
        "last_error_code": None,
        "loaded": True,
        "plugin": "governor",
        "policy": "tool_dispatch",
        "quarantined": False,
        "registered": True,
        "timeout_ms": 3750,
    }
    assert "TOP_SECRET_VALUE" not in output
    assert "callback" not in output.lower()


def test_policy_status_json_does_not_leak_plugin_load_error(
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from hermes_cli.plugins_cmd import cmd_policy_status

    _make_plugin(
        isolated_home,
        "governor",
        policies=["tool_dispatch"],
        init_source="raise RuntimeError('TOP_SECRET_PLUGIN_ERROR')\n",
    )
    _write_config(
        isolated_home,
        {
            "plugins": {
                "enabled": ["governor"],
                "required_policies": {"governor": ["tool_dispatch"]},
            }
        },
    )

    cmd_policy_status(SimpleNamespace(json=True))

    captured = capsys.readouterr()
    record = json.loads(captured.out)[0]
    assert record["enabled"] is True
    assert record["loaded"] is False
    assert record["registered"] is False
    assert record["last_error_code"] is None
    assert "TOP_SECRET_PLUGIN_ERROR" not in captured.out + captured.err


def test_policy_status_json_reports_missing_plugin(
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from hermes_cli.plugins_cmd import cmd_policy_status

    _write_config(
        isolated_home,
        {
            "plugins": {
                "required_policies": {"gone-plugin": ["tool_dispatch"]},
            }
        },
    )

    cmd_policy_status(SimpleNamespace(json=True))

    record = json.loads(capsys.readouterr().out)[0]
    assert record["installed"] is False
    assert record["enabled"] is False
    assert record["loaded"] is False
    assert record["registered"] is False


def test_policy_status_empty_json_is_an_empty_list(
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from hermes_cli.plugins_cmd import cmd_policy_status

    cmd_policy_status(SimpleNamespace(json=True))

    assert json.loads(capsys.readouterr().out) == []


def test_policy_status_malformed_config_exits_without_echoing_value(
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from hermes_cli.plugins_cmd import cmd_policy_status

    _write_config(
        isolated_home,
        {"plugins": {"required_policies": "TOP_SECRET_CONFIG_VALUE"}},
    )

    with pytest.raises(SystemExit) as exc_info:
        cmd_policy_status(SimpleNamespace(json=True))

    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert "Configuration error" in captured.err
    assert "TOP_SECRET_CONFIG_VALUE" not in captured.out + captured.err


def test_required_policy_parser_routes_all_new_actions() -> None:
    from hermes_cli.subcommands.plugins import build_plugins_parser

    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")

    def handler(args) -> None:
        return None

    build_plugins_parser(subparsers, cmd_plugins=handler)

    required = parser.parse_args([
        "plugins",
        "require-policy",
        "governor",
        "tool_dispatch",
    ])
    unrequired = parser.parse_args([
        "plugins",
        "unrequire-policy",
        "governor",
        "tool_dispatch",
    ])
    status = parser.parse_args(["plugins", "policy-status", "--json"])
    existing = parser.parse_args(["plugins", "disable", "governor"])

    assert required.plugins_action == "require-policy"
    assert required.plugin == "governor"
    assert required.policy == "tool_dispatch"
    assert unrequired.plugins_action == "unrequire-policy"
    assert status.plugins_action == "policy-status"
    assert status.json is True
    assert existing.plugins_action == "disable"
    assert all(
        item.func is handler for item in (required, unrequired, status, existing)
    )


@pytest.mark.parametrize(
    ("action", "target"),
    [
        ("require-policy", "cmd_require_policy"),
        ("unrequire-policy", "cmd_unrequire_policy"),
        ("policy-status", "cmd_policy_status"),
    ],
)
def test_plugins_command_dispatches_required_policy_actions(
    action: str,
    target: str,
) -> None:
    from hermes_cli.plugins_cmd import plugins_command

    args = SimpleNamespace(plugins_action=action)
    with patch(f"hermes_cli.plugins_cmd.{target}") as command:
        plugins_command(args)

    command.assert_called_once_with(args)
