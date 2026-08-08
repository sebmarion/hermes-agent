"""Focused contract tests for root-backed named-profile config overrides."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from hermes_cli import config as config_mod


@pytest.fixture()
def profile_tree(tmp_path, monkeypatch):
    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    config_mod._LOAD_CONFIG_CACHE.clear()
    config_mod._RAW_CONFIG_CACHE.clear()
    config_mod._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    yield root, profile
    config_mod._LOAD_CONFIG_CACHE.clear()
    config_mod._RAW_CONFIG_CACHE.clear()
    config_mod._LAST_EXPANDED_CONFIG_BY_PATH.clear()


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _bump_mtime(path: Path) -> None:
    stat_result = path.stat()
    os.utime(
        path,
        ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 1_000_000),
    )


def test_resolver_inherits_only_for_canonical_named_profile(tmp_path):
    root_config = tmp_path / "root" / "config.yaml"
    named_config = tmp_path / "root" / "profiles" / "worker" / "config.yaml"
    invalid_config = tmp_path / "root" / "profiles" / "Bad Name" / "config.yaml"

    root_layers = config_mod.resolve_config_layers(root_config)
    assert root_layers.inherits_root is False
    assert root_layers.root_config_path == root_config
    assert root_layers.override_config_path == root_config

    named_layers = config_mod.resolve_config_layers(named_config)
    assert named_layers.inherits_root is True
    assert named_layers.root_config_path == root_config
    assert named_layers.override_config_path == named_config
    assert named_layers.profile_name == "worker"

    invalid_layers = config_mod.resolve_config_layers(invalid_config)
    assert invalid_layers.inherits_root is False
    assert invalid_layers.root_config_path == invalid_config


def test_effective_raw_layers_root_and_child_but_physical_read_stays_local(
    profile_tree,
):
    root, profile = profile_tree
    root_path = root / "config.yaml"
    child_path = profile / "config.yaml"
    _write_yaml(
        root_path,
        {
            "model": {"provider": "root-provider", "default": "root-model"},
            "nested": {"from_root": True, "shared": "root"},
        },
    )
    _write_yaml(
        child_path,
        {
            "_profile": {"inherits": "default", "version": 1},
            "model": {"default": "child-model"},
            "nested": {"from_child": True, "shared": "child"},
        },
    )

    physical = config_mod.read_user_config_raw(child_path)
    effective_raw = config_mod.read_raw_config()

    assert physical["model"] == {"default": "child-model"}
    assert physical["nested"] == {"from_child": True, "shared": "child"}
    assert effective_raw["model"] == {
        "provider": "root-provider",
        "default": "child-model",
    }
    assert effective_raw["nested"] == {
        "from_root": True,
        "from_child": True,
        "shared": "child",
    }
    assert "_profile" not in effective_raw


def test_masks_remove_inherited_values_and_reveal_schema_defaults(profile_tree):
    root, profile = profile_tree
    _write_yaml(
        root / "config.yaml",
        {
            "display": {"compact": True},
            "custom": {"keep": "yes", "remove": "no"},
        },
    )
    _write_yaml(
        profile / "config.yaml",
        {
            "_profile": {
                "inherits": "default",
                "version": 1,
                "masks": [["display", "compact"], ["custom", "remove"]],
            }
        },
    )

    effective_raw = config_mod.read_raw_config()
    effective = config_mod.load_config()

    assert effective_raw["custom"] == {"keep": "yes"}
    assert "compact" not in effective_raw.get("display", {})
    assert effective["display"]["compact"] == config_mod.DEFAULT_CONFIG["display"]["compact"]
    assert effective["custom"] == {"keep": "yes"}


def test_invalid_profile_metadata_fails_closed(profile_tree):
    root, profile = profile_tree
    _write_yaml(root / "config.yaml", {"model": {"default": "root-model"}})
    _write_yaml(
        profile / "config.yaml",
        {"_profile": {"inherits": "another-profile", "version": 1}},
    )

    with pytest.raises(config_mod.ProfileConfigError, match="inherits"):
        config_mod.load_config()


def test_root_edits_invalidate_raw_readonly_and_effective_load_caches(profile_tree):
    root, _profile = profile_tree
    root_path = root / "config.yaml"
    _write_yaml(root_path, {"model": {"default": "root-one"}})

    first_raw = config_mod.read_raw_config_readonly()
    assert config_mod.read_raw_config_readonly() is first_raw
    assert first_raw["model"]["default"] == "root-one"
    assert config_mod.load_config()["model"]["default"] == "root-one"

    _write_yaml(root_path, {"model": {"default": "root-two"}})
    _bump_mtime(root_path)

    second_raw = config_mod.read_raw_config_readonly()
    assert second_raw is not first_raw
    assert second_raw["model"]["default"] == "root-two"
    assert config_mod.load_config()["model"]["default"] == "root-two"


def test_save_config_writes_only_named_profile_delta(profile_tree):
    root, profile = profile_tree
    root_path = root / "config.yaml"
    child_path = profile / "config.yaml"
    _write_yaml(
        root_path,
        {
            "_config_version": config_mod.DEFAULT_CONFIG["_config_version"],
            "model": {"provider": "root-provider", "default": "root-model"},
            "terminal": {"cwd": "/root-work"},
            "root_only": {"enabled": True},
        },
    )

    desired = config_mod.load_config()
    desired["model"]["default"] = "child-model"
    config_mod.save_config(desired)

    saved = yaml.safe_load(child_path.read_text(encoding="utf-8"))
    assert saved == {
        "_profile": {"inherits": "default", "version": 1},
        "model": {"default": "child-model"},
    }

    reloaded = config_mod.load_config()
    assert reloaded["model"] == {
        "provider": "root-provider",
        "default": "child-model",
    }
    assert reloaded["terminal"]["cwd"] == "/root-work"
    assert reloaded["root_only"] == {"enabled": True}


def test_save_preserves_masks_and_never_materializes_expanded_root_values(
    profile_tree, monkeypatch
):
    root, profile = profile_tree
    root_path = root / "config.yaml"
    child_path = profile / "config.yaml"
    monkeypatch.setenv("PROFILE_ENDPOINT", "https://secret.example.invalid")
    _write_yaml(
        root_path,
        {
            "endpoint": "${PROFILE_ENDPOINT}",
            "display": {"compact": True},
            "custom": {"remove": "root", "keep": "root"},
        },
    )
    _write_yaml(
        child_path,
        {
            "_profile": {
                "inherits": "default",
                "version": 1,
                "masks": [["display", "compact"], ["custom", "remove"]],
            },
            "profile_local": {"label": "before"},
        },
    )

    desired = config_mod.load_config()
    assert desired["endpoint"] == "https://secret.example.invalid"
    desired["profile_local"]["label"] = "after"
    config_mod.save_config(desired)

    saved_text = child_path.read_text(encoding="utf-8")
    saved = yaml.safe_load(saved_text)
    assert "secret.example.invalid" not in saved_text
    assert saved["_profile"]["masks"] == [
        ["custom", "remove"],
        ["display", "compact"],
    ]
    assert saved["profile_local"] == {"label": "after"}
    assert "endpoint" not in saved
    assert config_mod.load_config()["custom"] == {"keep": "root"}


def test_root_and_named_profile_share_one_machine_write_lock(tmp_path):
    root_config = tmp_path / "root" / "config.yaml"
    child_config = tmp_path / "root" / "profiles" / "worker" / "config.yaml"

    assert config_mod.config_write_lock_path(root_config) == config_mod.config_write_lock_path(
        child_config
    )


def test_explicit_path_reader_and_projection_do_not_require_home_switch(tmp_path):
    root_path = tmp_path / "root" / "config.yaml"
    child_path = tmp_path / "root" / "profiles" / "worker" / "config.yaml"
    _write_yaml(
        root_path,
        {
            "model": {"provider": "root-provider", "default": "root-model"},
            "nested": {"keep": True, "remove": True},
        },
    )
    _write_yaml(child_path, {})

    projected = config_mod.project_profile_override(
        child_path,
        {
            "model": {"provider": "root-provider", "default": "child-model"},
            "nested": {"keep": True},
        },
    )
    _write_yaml(child_path, projected)

    assert projected == {
        "_profile": {
            "inherits": "default",
            "version": 1,
            "masks": [["nested", "remove"]],
        },
        "model": {"default": "child-model"},
    }
    assert config_mod.read_effective_user_config_for_path(child_path) == {
        "model": {"provider": "root-provider", "default": "child-model"},
        "nested": {"keep": True},
    }
