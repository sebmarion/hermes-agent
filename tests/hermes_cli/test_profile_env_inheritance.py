"""Contract tests for root-backed named-profile dotenv overlays."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from agent import secret_scope
from hermes_cli import config as config_mod
from hermes_cli import env_loader


@pytest.fixture()
def profile_tree(tmp_path, monkeypatch):
    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    config_mod.invalidate_env_cache()
    env_loader.reset_secret_source_cache()
    getattr(env_loader, "_PROFILE_INHERITED_ENV_VALUES", {}).clear()
    yield root, profile
    config_mod.invalidate_env_cache()
    env_loader.reset_secret_source_cache()
    getattr(env_loader, "_PROFILE_INHERITED_ENV_VALUES", {}).clear()


def _write_profile_configs(root: Path, profile: Path) -> None:
    (root / "config.yaml").write_text("{}\n", encoding="utf-8")
    (profile / "config.yaml").write_text(
        yaml.safe_dump(
            {"_profile": {"inherits": "default", "version": 1}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _bump_mtime(path: Path) -> None:
    stat_result = path.stat()
    os.utime(
        path,
        ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 1_000_000),
    )


def test_load_env_merges_root_then_profile_and_invalidates_on_root_edit(
    profile_tree,
):
    root, profile = profile_tree
    _write_profile_configs(root, profile)
    root_env = root / ".env"
    root_env.write_text(
        "NOVITA_API_KEY=root-novita\n"
        "NOVITA_BASE_URL=https://root.novita.invalid/v1\n"
        "ROOT_PRIVATE_SECRET=must-not-inherit\n",
        encoding="utf-8",
    )
    (profile / ".env").write_text(
        "NOVITA_BASE_URL=\nPROFILE_ONLY=worker\n",
        encoding="utf-8",
    )

    assert config_mod.load_env() == {
        "NOVITA_API_KEY": "root-novita",
        "NOVITA_BASE_URL": "",
        "PROFILE_ONLY": "worker",
    }

    root_env.write_text(
        "NOVITA_API_KEY=next-novita\n"
        "NOVITA_BASE_URL=https://root.novita.invalid/v1\n"
        "ROOT_PRIVATE_SECRET=must-not-inherit\n",
        encoding="utf-8",
    )
    _bump_mtime(root_env)

    assert config_mod.load_env()["NOVITA_API_KEY"] == "next-novita"


def test_build_profile_secret_scope_uses_layered_files_without_process_fallback(
    profile_tree, monkeypatch
):
    root, profile = profile_tree
    _write_profile_configs(root, profile)
    (root / ".env").write_text(
        "NOVITA_API_KEY=root-novita\n"
        "NOVITA_BASE_URL=https://root.novita.invalid/v1\n"
        "ROOT_PRIVATE_SECRET=must-not-inherit\n",
        encoding="utf-8",
    )
    (profile / ".env").write_text(
        "NOVITA_BASE_URL=\nPROFILE_ONLY=worker\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PROCESS_ONLY_SECRET", "must-not-leak")

    assert secret_scope.build_profile_secret_scope(profile) == {
        "NOVITA_API_KEY": "root-novita",
        "NOVITA_BASE_URL": "",
        "PROFILE_ONLY": "worker",
    }


def test_load_hermes_dotenv_loads_novita_only_and_excludes_root_routing_key(
    profile_tree, monkeypatch
):
    root, profile = profile_tree
    _write_profile_configs(root, profile)
    root_env = root / ".env"
    profile_env = profile / ".env"
    root_env.write_text(
        "NOVITA_API_KEY=root-novita\n"
        "NOVITA_BASE_URL=https://root.novita.invalid/v1\n"
        "ROOT_PRIVATE_SECRET=must-not-inherit\n"
        "HERMES_ACP_AUTH_METHOD=must-not-inherit\n",
        encoding="utf-8",
    )
    profile_env.write_text(
        "NOVITA_BASE_URL=\nPROFILE_ONLY=worker\n",
        encoding="utf-8",
    )
    for name in (
        "NOVITA_API_KEY",
        "NOVITA_BASE_URL",
        "PROFILE_ONLY",
        "ROOT_PRIVATE_SECRET",
        "HERMES_ACP_AUTH_METHOD",
    ):
        monkeypatch.delenv(name, raising=False)

    loaded = env_loader.load_hermes_dotenv(hermes_home=profile)

    assert loaded == [root_env, profile_env]
    assert os.environ["NOVITA_API_KEY"] == "root-novita"
    assert os.environ["NOVITA_BASE_URL"] == ""
    assert os.environ["PROFILE_ONLY"] == "worker"
    assert "ROOT_PRIVATE_SECRET" not in os.environ
    assert "HERMES_ACP_AUTH_METHOD" not in os.environ


def test_load_hermes_dotenv_removes_deleted_loader_owned_inheritance(
    profile_tree, monkeypatch
):
    root, profile = profile_tree
    root_env = root / ".env"
    root_env.write_text("NOVITA_API_KEY=root-novita\n", encoding="utf-8")
    (profile / ".env").write_text("PROFILE_ONLY=worker\n", encoding="utf-8")
    monkeypatch.delenv("NOVITA_API_KEY", raising=False)

    env_loader.load_hermes_dotenv(hermes_home=profile)
    assert os.environ["NOVITA_API_KEY"] == "root-novita"

    root_env.write_text("ROOT_PRIVATE_SECRET=local-only\n", encoding="utf-8")
    env_loader.load_hermes_dotenv(hermes_home=profile)

    assert "NOVITA_API_KEY" not in os.environ


def test_load_hermes_dotenv_preserves_later_shell_owner_on_root_delete(
    profile_tree, monkeypatch
):
    root, profile = profile_tree
    root_env = root / ".env"
    root_env.write_text("NOVITA_API_KEY=root-novita\n", encoding="utf-8")
    (profile / ".env").write_text("PROFILE_ONLY=worker\n", encoding="utf-8")
    monkeypatch.delenv("NOVITA_API_KEY", raising=False)

    env_loader.load_hermes_dotenv(hermes_home=profile)
    os.environ["NOVITA_API_KEY"] = "later-shell-owner"
    root_env.write_text("ROOT_PRIVATE_SECRET=local-only\n", encoding="utf-8")

    env_loader.load_hermes_dotenv(hermes_home=profile)

    assert os.environ["NOVITA_API_KEY"] == "later-shell-owner"


def test_load_hermes_dotenv_clears_inheritance_when_switching_to_standalone(
    profile_tree, tmp_path, monkeypatch
):
    root, profile = profile_tree
    (root / ".env").write_text("NOVITA_API_KEY=root-novita\n", encoding="utf-8")
    (profile / ".env").write_text("PROFILE_ONLY=worker\n", encoding="utf-8")
    standalone = tmp_path / "standalone-home"
    standalone.mkdir()
    (standalone / ".env").write_text("STANDALONE_ONLY=yes\n", encoding="utf-8")
    monkeypatch.delenv("NOVITA_API_KEY", raising=False)

    env_loader.load_hermes_dotenv(hermes_home=profile)
    env_loader.load_hermes_dotenv(hermes_home=standalone)

    assert "NOVITA_API_KEY" not in os.environ
    assert os.environ["STANDALONE_ONLY"] == "yes"


def test_noncanonical_home_remains_standalone(tmp_path, monkeypatch):
    root = tmp_path / "hermes-root"
    standalone = tmp_path / "standalone"
    root.mkdir()
    standalone.mkdir()
    (root / ".env").write_text("NOVITA_API_KEY=root-novita\n", encoding="utf-8")
    (standalone / ".env").write_text("PROFILE_ONLY=standalone\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(standalone))
    config_mod.invalidate_env_cache()

    assert config_mod.load_env() == {"PROFILE_ONLY": "standalone"}
    assert secret_scope.build_profile_secret_scope(standalone) == {
        "PROFILE_ONLY": "standalone"
    }


def test_lexical_profile_inheritance_does_not_require_marker_or_config(tmp_path, monkeypatch):
    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "legacy"
    profile.mkdir(parents=True)
    (root / ".env").write_text("NOVITA_API_KEY=root-novita\n", encoding="utf-8")
    (profile / ".env").write_text("PROFILE_ONLY=legacy\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    config_mod.invalidate_env_cache()

    assert config_mod.load_env() == {
        "NOVITA_API_KEY": "root-novita",
        "PROFILE_ONLY": "legacy",
    }


def test_inherited_novita_runtime_does_not_materialize_child_pool(
    profile_tree, monkeypatch
):
    root, profile = profile_tree
    (root / "config.yaml").write_text(
        yaml.safe_dump(
            {"model": {"provider": "novita", "default": "zai-org/glm-5.2"}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (profile / "config.yaml").write_text(
        yaml.safe_dump(
            {"_profile": {"inherits": "default", "version": 1}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (root / ".env").write_text(
        "NOVITA_API_KEY=root-novita\n"
        "NOVITA_BASE_URL=https://root.novita.invalid/v1\n",
        encoding="utf-8",
    )
    (profile / ".env").write_text(
        "NOVITA_BASE_URL=https://child.novita.invalid/v1\n"
        "PROFILE_ONLY=worker\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("NOVITA_BASE_URL", "https://hostile-process.invalid/v1")
    auth_path = profile / "auth.json"
    auth_path.write_text(
        json.dumps({"version": 1, "providers": {}, "credential_pool": {}}),
        encoding="utf-8",
    )
    config_mod.invalidate_env_cache()

    from agent.credential_pool import load_pool
    from hermes_cli.auth import resolve_api_key_provider_credentials
    from hermes_cli.runtime_provider import resolve_runtime_provider

    assert load_pool("novita").entries() == []
    assert (
        config_mod.get_env_value_prefer_dotenv("NOVITA_BASE_URL")
        == "https://child.novita.invalid/v1"
    )
    assert (
        resolve_api_key_provider_credentials("novita")["base_url"]
        == "https://child.novita.invalid/v1"
    )
    runtime = resolve_runtime_provider(
        requested="novita",
        target_model="zai-org/glm-5.2",
    )

    assert runtime["provider"] == "novita"
    assert runtime["api_key"] == "root-novita"
    assert runtime["base_url"] == "https://child.novita.invalid/v1"
    stored = json.loads(auth_path.read_text(encoding="utf-8"))
    assert "novita" not in stored.get("credential_pool", {})


def test_local_novita_override_remains_seedable_in_child_pool(profile_tree):
    root, profile = profile_tree
    _write_profile_configs(root, profile)
    (root / ".env").write_text("NOVITA_API_KEY=root-novita\n", encoding="utf-8")
    (profile / ".env").write_text(
        "NOVITA_API_KEY=child-novita\n", encoding="utf-8"
    )
    (profile / "auth.json").write_text(
        json.dumps({"version": 1, "providers": {}, "credential_pool": {}}),
        encoding="utf-8",
    )
    config_mod.invalidate_env_cache()

    from agent.credential_pool import load_pool

    entries = load_pool("novita").entries()

    assert [entry.source for entry in entries] == ["env:NOVITA_API_KEY"]


def test_inherited_novita_prunes_stale_child_env_pool_row(profile_tree):
    root, profile = profile_tree
    _write_profile_configs(root, profile)
    (root / ".env").write_text("NOVITA_API_KEY=root-novita\n", encoding="utf-8")
    (profile / ".env").write_text("PROFILE_ONLY=worker\n", encoding="utf-8")
    auth_path = profile / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "version": 1,
                "providers": {},
                "credential_pool": {
                    "novita": [
                        {
                            "id": "stale-child-env",
                            "label": "NOVITA_API_KEY",
                            "auth_type": "api_key",
                            "priority": 0,
                            "source": "env:NOVITA_API_KEY",
                            "access_token": "stale-child-novita",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    config_mod.invalidate_env_cache()

    from agent.credential_pool import load_pool

    assert load_pool("novita").entries() == []
    stored = json.loads(auth_path.read_text(encoding="utf-8"))
    assert stored.get("credential_pool", {}).get("novita", []) == []


def test_empty_child_novita_mask_prunes_stale_child_env_pool_row(profile_tree):
    root, profile = profile_tree
    _write_profile_configs(root, profile)
    (root / ".env").write_text("NOVITA_API_KEY=root-novita\n", encoding="utf-8")
    (profile / ".env").write_text("NOVITA_API_KEY=\n", encoding="utf-8")
    auth_path = profile / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "version": 1,
                "providers": {},
                "credential_pool": {
                    "novita": [
                        {
                            "id": "stale-child-env",
                            "label": "NOVITA_API_KEY",
                            "auth_type": "api_key",
                            "priority": 0,
                            "source": "env:NOVITA_API_KEY",
                            "access_token": "stale-child-novita",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    config_mod.invalidate_env_cache()

    from agent.credential_pool import load_pool

    assert load_pool("novita").entries() == []
    stored = json.loads(auth_path.read_text(encoding="utf-8"))
    assert stored.get("credential_pool", {}).get("novita", []) == []


def test_novita_pricing_honors_child_empty_key_mask(profile_tree, monkeypatch):
    root, profile = profile_tree
    _write_profile_configs(root, profile)
    (root / ".env").write_text(
        "NOVITA_API_KEY=root-novita\n"
        "NOVITA_BASE_URL=https://root.novita.invalid/v1\n",
        encoding="utf-8",
    )
    (profile / ".env").write_text(
        "NOVITA_API_KEY=\n"
        "NOVITA_BASE_URL=https://child.novita.invalid/v1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("NOVITA_API_KEY", "hostile-process-key")
    config_mod.invalidate_env_cache()

    from hermes_cli import models as models_mod

    models_mod._pricing_cache.clear()
    requests = []

    def _unexpected_request(*_args, **_kwargs):
        requests.append(True)
        raise AssertionError("masked Novita key must not make a network request")

    monkeypatch.setattr(
        models_mod, "_urlopen_model_catalog_request", _unexpected_request
    )

    assert models_mod._fetch_novita_pricing(force_refresh=True) == {}
    assert requests == []
