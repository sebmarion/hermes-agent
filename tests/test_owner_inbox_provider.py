from types import SimpleNamespace

from hermes_cli.plugins import PluginContext, PluginManager


def test_plugin_can_register_generic_owner_inbox_provider(tmp_path):
    manager = PluginManager(scope_key=str(tmp_path))
    manifest = SimpleNamespace(name="example", key="example")
    context = PluginContext(manifest, manager)
    seen = []
    provider = lambda owner: seen.append(owner)

    context.register_owner_inbox_provider(provider)

    providers = manager.owner_inbox_providers()
    assert len(providers) == 1
    providers[0]("owner")
    assert seen == ["owner"]


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


def test_platform_lifecycle_continues_after_sync_and_async_failures():
    import asyncio
    from gateway.platforms.base import BasePlatformAdapter
    class Adapter(BasePlatformAdapter):
        async def connect(self): pass
        async def disconnect(self): pass
        async def send(self, *args, **kwargs): pass
        async def get_chat_info(self, *args, **kwargs): pass
    adapter = object.__new__(Adapter)
    events = []
    def bad(_): events.append("bad") or (_ for _ in ()).throw(RuntimeError("x"))
    async def bad_async(_): events.append("bad_async"); raise RuntimeError("y")
    def good(_): events.append("good")
    adapter._plugin_lifecycle_handles = [("app", {"on_ready": bad}),
                                         ("app", {"on_ready": bad_async}),
                                         ("app", {"on_ready": good})]
    asyncio.run(adapter._start_plugin_lifecycle("app"))
    assert events == ["bad", "bad_async", "good"]
    adapter._plugin_lifecycle_handles = [("app", {"on_stop": bad}),
                                         ("app", {"on_stop": good})]
    asyncio.run(adapter._stop_plugin_lifecycle("app"))
    assert events[-2:] == ["bad", "good"]
    assert adapter._plugin_lifecycle_handles == []