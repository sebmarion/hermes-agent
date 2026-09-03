from types import SimpleNamespace

from hermes_cli.plugins import PluginContext, PluginManager


def test_plugin_can_register_generic_owner_inbox_provider(tmp_path):
    manager = PluginManager(scope_key=str(tmp_path))
    manifest = SimpleNamespace(name="example", key="example")
    context = PluginContext(manifest, manager)
    provider = lambda owner: None

    context.register_owner_inbox_provider(provider)

    assert manager.owner_inbox_providers() == [provider]


def test_owner_startup_discovers_and_starts_providers(monkeypatch):
    import hermes_cli.plugins as plugins
    from tui_gateway.owner_inbox import start_registered_owner_inboxes
    events = []
    stop = lambda: events.append("stop")
    provider = lambda owner: events.append("provider") or stop
    monkeypatch.setattr(plugins, "discover_plugins", lambda: events.append("discover"))
    monkeypatch.setattr(plugins, "get_owner_inbox_providers", lambda: [provider])
    class Server:
        _sessions_lock = __import__("threading").RLock()
        _sessions = {}
        _current_profile_name = staticmethod(lambda: "default")
    stops = start_registered_owner_inboxes(Server, lambda params: {})
    assert events == ["discover", "provider"]
    assert len(stops) == 1