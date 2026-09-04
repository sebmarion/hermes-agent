from types import SimpleNamespace

from hermes_cli.plugins import PluginContext, PluginManager


def _manager_with_alpha_and_beta(tmp_path):
    manager = PluginManager(scope_key=str(tmp_path))
    alpha = SimpleNamespace(name="alpha", key="alpha")
    beta = SimpleNamespace(name="beta", key="beta")
    alpha_provider = lambda owner: "alpha"
    beta_provider = lambda owner: "beta"
    alpha_observer = lambda event: "alpha"
    beta_observer = lambda event: "beta"
    PluginContext(alpha, manager).register_owner_inbox_provider(alpha_provider)
    PluginContext(beta, manager).register_owner_inbox_provider(beta_provider)
    PluginContext(alpha, manager).register_prompt_admission_observer(alpha_observer)
    PluginContext(beta, manager).register_prompt_admission_observer(beta_observer)
    manager._plugins["alpha"] = SimpleNamespace(manifest=alpha, enabled=True)
    return manager, alpha, beta_provider, beta_observer


def test_scoped_unload_removes_alpha_but_preserves_beta(tmp_path):
    manager, _alpha, beta_provider, beta_observer = _manager_with_alpha_and_beta(tmp_path)
    assert manager.unload("alpha") is True
    remaining_providers = manager.owner_inbox_providers()
    assert len(remaining_providers) == 1
    assert remaining_providers[0](None) == beta_provider(None)
    assert manager.prompt_admission_observers() == [beta_observer]


def test_scoped_unload_stops_started_owner_provider_once(tmp_path):
    manager = PluginManager(scope_key=str(tmp_path))
    manifest = SimpleNamespace(name="alpha", key="alpha")
    stop_calls = []

    def provider(_owner):
        return lambda: stop_calls.append("stopped")

    PluginContext(manifest, manager).register_owner_inbox_provider(provider)
    manager._plugins["alpha"] = SimpleNamespace(manifest=manifest, enabled=True)
    normal_shutdown_stop = manager.owner_inbox_providers()[0](None)

    assert manager.unload("alpha") is True
    assert stop_calls == ["stopped"]

    normal_shutdown_stop()
    assert stop_calls == ["stopped"]


def test_released_provider_handle_does_not_start_again(tmp_path):
    manager = PluginManager(scope_key=str(tmp_path))
    manifest = SimpleNamespace(name="alpha", key="alpha")
    starts = []

    def provider(owner):
        starts.append(owner)
        return lambda: None

    PluginContext(manifest, manager).register_owner_inbox_provider(provider)
    manager._plugins["alpha"] = SimpleNamespace(manifest=manifest, enabled=True)
    managed_provider = manager.owner_inbox_providers()[0]

    assert manager.unload("alpha") is True
    assert managed_provider("owner") is None
    assert starts == []


def test_force_reload_does_not_duplicate_callbacks(tmp_path):
    manager, alpha, _beta_provider, _beta_observer = _manager_with_alpha_and_beta(tmp_path)
    manager._discovered = True

    def rediscover():
        PluginContext(alpha, manager).register_owner_inbox_provider(
            lambda owner: "rediscovered"
        )
        PluginContext(alpha, manager).register_prompt_admission_observer(
            lambda event: "rediscovered"
        )

    manager._discover_and_load_inner = rediscover
    manager.discover_and_load(force=True)
    assert len(manager.owner_inbox_providers()) == 1
    assert len(manager.prompt_admission_observers()) == 1


def test_unload_all_clears_callbacks(tmp_path):
    manager, _alpha, _beta_provider, _beta_observer = _manager_with_alpha_and_beta(tmp_path)
    assert manager.unload() is True
    assert manager.owner_inbox_providers() == []
    assert manager.prompt_admission_observers() == []
