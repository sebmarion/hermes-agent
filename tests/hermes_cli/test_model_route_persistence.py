from __future__ import annotations

import inspect
import os
import stat

import pytest
import yaml

import hermes_cli.config as config_mod
from hermes_cli import managed_scope
import utils


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_MANAGED", raising=False)
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    managed_scope.invalidate_managed_cache()
    return home


def _persist(**kwargs):
    return config_mod.persist_main_model_assignment(**kwargs)


def _spy_atomic_updates(monkeypatch):
    calls = []
    original = utils.atomic_roundtrip_yaml_updates

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(utils, "atomic_roundtrip_yaml_updates", spy)
    return calls


def test_custom_to_codex_removes_stale_endpoint_fields_and_preserves_comments(
    config_home, monkeypatch
):
    path = config_home / "config.yaml"
    path.write_text(
        "# root comment\n"
        "model:\n"
        "  # route comment\n"
        "  provider: custom\n"
        "  default: NeuralWatt\n"
        "  base_url: https://neuralwatt.example/v1\n"
        "  api_mode: anthropic_messages\n"
        "  api_key: stored-old-key\n"
        "  api: stored-legacy-key\n"
        "  sibling: keep-me  # sibling comment\n"
        "other: keep-root  # root sibling comment\n",
        encoding="utf-8",
    )
    calls = _spy_atomic_updates(monkeypatch)

    _persist(provider="openai-codex", model="gpt-5.4")

    written_text = path.read_text(encoding="utf-8")
    written = yaml.safe_load(written_text)
    assert written["model"] == {
        "provider": "openai-codex",
        "default": "gpt-5.4",
        "sibling": "keep-me",
    }
    assert written["other"] == "keep-root"
    assert "# root comment" in written_text
    assert "# route comment" in written_text
    assert "# sibling comment" in written_text
    assert "# root sibling comment" in written_text
    assert written_text.index("model:") < written_text.index("other:")
    assert len(calls) == 1


def test_legacy_route_aliases_are_removed_atomically_and_cannot_reappear_on_reload(
    config_home, monkeypatch
):
    path = config_home / "config.yaml"
    path.write_text(
        "# unrelated root comment\n"
        "provider: custom\n"
        "base_url: https://root-stale.example/v1\n"
        "api_base: https://root-alias-stale.example/v1\n"
        "context_length: 131072\n"
        "model:\n"
        "  model: stale-model-alias\n"
        "  name: stale-name-alias\n"
        "  api_base: https://nested-alias-stale.example/v1\n"
        "  api_mode: anthropic_messages\n"
        "  api_key: stored-old-key\n"
        "  api: stored-legacy-key\n"
        "  sibling: keep-me  # sibling comment\n"
        "other: keep-root  # root sibling comment\n",
        encoding="utf-8",
    )
    calls = _spy_atomic_updates(monkeypatch)

    _persist(provider="openai-codex", model="gpt-5.4")

    written_text = path.read_text(encoding="utf-8")
    written = yaml.safe_load(written_text)
    assert written["model"] == {
        "provider": "openai-codex",
        "default": "gpt-5.4",
        "sibling": "keep-me",
    }
    assert written["other"] == "keep-root"
    assert all(
        key not in written
        for key in ("provider", "base_url", "api_base", "context_length")
    )
    assert all(
        key not in written["model"]
        for key in (
            "model",
            "name",
            "api_base",
            "base_url",
            "context_length",
            "api_mode",
            "api_key",
            "api",
        )
    )
    assert "# unrelated root comment" in written_text
    assert "# sibling comment" in written_text
    assert "# root sibling comment" in written_text
    assert len(calls) == 1

    reloaded_model = config_mod.load_config()["model"]
    assert reloaded_model["provider"] == "openai-codex"
    assert reloaded_model["default"] == "gpt-5.4"
    assert all(
        key not in reloaded_model
        for key in (
            "model",
            "name",
            "api_base",
            "base_url",
            "context_length",
            "api_mode",
            "api_key",
            "api",
        )
    )


def test_custom_switch_persists_complete_route_without_accepting_runtime_api_key(
    config_home
):
    path = config_home / "config.yaml"
    path.write_text(
        "model:\n"
        "  provider: openai-codex\n"
        "  default: gpt-5.4\n",
        encoding="utf-8",
    )

    assert "api_key" not in inspect.signature(
        config_mod.persist_main_model_assignment
    ).parameters
    _persist(
        provider="custom",
        model="neuralwatt-v2",
        base_url="https://neuralwatt.example/v1",
        api_mode="chat_completions",
    )

    written = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert written["model"] == {
        "provider": "custom",
        "default": "neuralwatt-v2",
        "base_url": "https://neuralwatt.example/v1",
        "api_mode": "chat_completions",
    }


def test_same_provider_model_change_preserves_existing_custom_endpoint(config_home):
    path = config_home / "config.yaml"
    path.write_text(
        "model:\n"
        "  provider: custom\n"
        "  default: old-model\n"
        "  base_url: https://custom.example/v1\n"
        "  api_mode: anthropic_messages\n"
        "  api_key: explicitly-stored-key\n",
        encoding="utf-8",
    )

    _persist(provider="custom", model="new-model")

    written = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert written["model"] == {
        "provider": "custom",
        "default": "new-model",
        "base_url": "https://custom.example/v1",
        "api_mode": "anthropic_messages",
        "api_key": "explicitly-stored-key",
    }


@pytest.mark.parametrize(
    "body",
    [
        "other: keep\n",
        "model:\nother: keep\n",
        "model: old-model\nother: keep\n",
        "model: 42\nother: keep\n",
    ],
    ids=["missing", "null", "string", "scalar"],
)
def test_non_mapping_model_shapes_become_complete_mapping(config_home, body):
    path = config_home / "config.yaml"
    path.write_text(body, encoding="utf-8")

    _persist(provider="openrouter", model="openai/gpt-5.4")

    written = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert written["model"] == {
        "provider": "openrouter",
        "default": "openai/gpt-5.4",
    }
    assert written["other"] == "keep"


@pytest.mark.parametrize(
    "managed_body",
    [
        "model: managed-model\n",
        "model:\n  default: managed-model\n",
        "model:\n  api_mode: responses\n",
    ],
    ids=["model-root", "model-default", "model-api-mode"],
)
def test_managed_route_key_rejects_entire_transaction_with_zero_writes(
    config_home, monkeypatch, tmp_path, managed_body
):
    path = config_home / "config.yaml"
    path.write_text(
        "model:\n  provider: custom\n  default: old\n"
        "  base_url: https://old.example/v1\n",
        encoding="utf-8",
    )
    before = path.read_bytes()
    managed_dir = tmp_path / "managed"
    managed_dir.mkdir()
    (managed_dir / "config.yaml").write_text(managed_body, encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
    managed_scope.invalidate_managed_cache()
    calls = _spy_atomic_updates(monkeypatch)

    with pytest.raises(RuntimeError, match="managed"):
        _persist(provider="openai-codex", model="gpt-5.4")

    assert calls == []
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "managed_body",
    [
        "provider: managed-provider\n",
        "base_url: https://managed.example/v1\n",
        "context_length: 123456\n",
        "api_base: https://managed-alias.example/v1\n",
        "model:\n  api_base: https://managed-model-alias.example/v1\n",
        "model:\n  model: managed-model-alias\n",
        "model:\n  name: managed-name-alias\n",
        "model: managed-scalar-model\n",
    ],
    ids=[
        "root-provider",
        "root-base-url",
        "root-context-length",
        "root-api-base",
        "model-api-base",
        "model-model",
        "model-name",
        "root-model-scalar",
    ],
)
def test_managed_route_alias_rejects_entire_transaction_with_zero_writes(
    config_home, monkeypatch, tmp_path, managed_body
):
    path = config_home / "config.yaml"
    path.write_text(
        "model:\n"
        "  provider: custom\n"
        "  default: old\n"
        "  base_url: https://old.example/v1\n"
        "  context_length: 64000\n",
        encoding="utf-8",
    )
    before = path.read_bytes()
    managed_dir = tmp_path / "managed"
    managed_dir.mkdir()
    (managed_dir / "config.yaml").write_text(managed_body, encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
    managed_scope.invalidate_managed_cache()
    calls = _spy_atomic_updates(monkeypatch)

    with pytest.raises(RuntimeError, match="managed"):
        _persist(provider="openai-codex", model="gpt-5.4")

    assert calls == []
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("managed_key", "managed_value"),
    [
        ("provider", "managed-provider"),
        ("default", "managed-model"),
        ("base_url", "https://managed.example/v1"),
        ("api_mode", "managed_mode"),
        ("api_key", "managed-key"),
        ("api", "managed-legacy-key"),
        ("context_length", 123456),
    ],
    ids=[
        "model-provider",
        "model-default",
        "model-base-url",
        "model-api-mode",
        "model-api-key",
        "model-api",
        "model-context-length",
    ],
)
def test_managed_nested_touched_key_rejects_with_zero_writes(
    config_home, monkeypatch, tmp_path, managed_key, managed_value
):
    path = config_home / "config.yaml"
    path.write_text(
        "model:\n  provider: custom\n  default: old\n",
        encoding="utf-8",
    )
    before = path.read_bytes()
    managed_dir = tmp_path / "managed"
    managed_dir.mkdir()
    (managed_dir / "config.yaml").write_text(
        yaml.safe_dump({"model": {managed_key: managed_value}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
    managed_scope.invalidate_managed_cache()
    calls = _spy_atomic_updates(monkeypatch)

    with pytest.raises(RuntimeError, match="managed"):
        _persist(provider="openai-codex", model="gpt-5.4")

    assert calls == []
    assert path.read_bytes() == before


def test_fully_managed_install_rejects_with_zero_writes(
    config_home, monkeypatch
):
    path = config_home / "config.yaml"
    path.write_text("model:\n  default: old\n", encoding="utf-8")
    before = path.read_bytes()
    # ``homebrew`` is a historical value that is deliberately ignored; the
    # fully-managed lock is represented by the supported truthy signal.
    monkeypatch.setenv("HERMES_MANAGED", "true")
    calls = _spy_atomic_updates(monkeypatch)

    with pytest.raises(RuntimeError, match="managed"):
        _persist(provider="openai-codex", model="gpt-5.4")

    assert calls == []
    assert path.read_bytes() == before


def test_invalid_existing_config_fails_closed_with_zero_writes(
    config_home, monkeypatch
):
    path = config_home / "config.yaml"
    path.write_text("model: [unterminated\n", encoding="utf-8")
    before = path.read_bytes()
    calls = _spy_atomic_updates(monkeypatch)

    with pytest.raises(RuntimeError, match="parse|invalid|read"):
        _persist(provider="openai-codex", model="gpt-5.4")

    assert calls == []
    assert path.read_bytes() == before


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX unreadable-file semantics require a non-root process",
)
def test_unreadable_existing_config_fails_closed_with_zero_writes(
    config_home, monkeypatch
):
    path = config_home / "config.yaml"
    path.write_text("model:\n  default: old\n", encoding="utf-8")
    path.chmod(0)
    calls = _spy_atomic_updates(monkeypatch)
    try:
        with pytest.raises(RuntimeError, match="cannot be read"):
            _persist(provider="openai-codex", model="gpt-5.4")
        assert calls == []
    finally:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes do not apply on Windows")
def test_atomic_route_update_preserves_existing_file_mode(config_home):
    path = config_home / "config.yaml"
    path.write_text("model:\n  default: old\n", encoding="utf-8")
    path.chmod(0o640)

    _persist(provider="openai-codex", model="gpt-5.4")

    assert stat.S_IMODE(path.stat().st_mode) == 0o640
