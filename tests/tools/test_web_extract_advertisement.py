"""Planner-visible web_extract must match the active provider capability."""

from __future__ import annotations

from agent.web_search_provider import WebSearchProvider


class _MutableProvider(WebSearchProvider):
    def __init__(self, name: str, *, extract: bool, available: bool = True):
        self._name = name
        self._extract = extract
        self.available = available

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return self.available

    def supports_extract(self) -> bool:
        return self._extract


def _reset_web_state():
    from agent.web_search_registry import _reset_for_tests
    from model_tools import _clear_tool_defs_cache
    from tools.registry import invalidate_check_fn_cache

    _reset_for_tests()
    invalidate_check_fn_cache()
    _clear_tool_defs_cache()


def test_search_only_backend_does_not_advertise_web_extract(monkeypatch):
    from agent.web_search_registry import register_provider
    from tools import web_tools

    _reset_web_state()
    register_provider(_MutableProvider("search-only", extract=False))
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "search-only"})

    try:
        assert web_tools.check_web_search_available() is True
        assert web_tools.check_web_extract_available() is False
    finally:
        _reset_web_state()


def test_extract_capable_backend_is_advertised(monkeypatch):
    from agent.web_search_registry import register_provider
    from tools import web_tools

    _reset_web_state()
    register_provider(_MutableProvider("extractor", extract=True))
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "extractor"})

    try:
        assert web_tools.check_web_extract_available() is True
    finally:
        _reset_web_state()


def test_web_extract_gate_reflects_credential_loss_without_ttl_grace(monkeypatch):
    from agent.web_search_registry import register_provider
    from tools import web_tools
    from tools.registry import _check_fn_cached

    _reset_web_state()
    provider = _MutableProvider("extractor", extract=True)
    register_provider(provider)
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "extractor"})

    try:
        assert _check_fn_cached(web_tools.check_web_extract_available) is True
        provider.available = False
        assert _check_fn_cached(web_tools.check_web_extract_available) is False
    finally:
        _reset_web_state()


def test_registry_uses_capability_specific_gates():
    from tools import web_tools
    from tools.registry import registry

    assert registry.get_entry("web_search").check_fn is web_tools.check_web_search_available
    assert registry.get_entry("web_extract").check_fn is web_tools.check_web_extract_available


def test_web_capability_fingerprint_changes_on_provider_availability(monkeypatch):
    from agent.web_search_registry import register_provider
    from tools import web_tools

    _reset_web_state()
    provider = _MutableProvider("extractor", extract=True)
    register_provider(provider)
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "extractor"})

    try:
        before = web_tools.web_capability_fingerprint()
        provider.available = False
        after = web_tools.web_capability_fingerprint()
        assert before != after
    finally:
        _reset_web_state()
