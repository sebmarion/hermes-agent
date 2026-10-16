"""Child-worker routing policy seam (migration bridge, Phase 6).

Verifies the generic plugin-host seam added to tools/delegate_tool.py:
- A registered policy may map lanes/default_lane to a provider+model (authoritative).
- Core resolves/validates secrets itself via resolve_runtime_provider (policy never
  sees API keys).
- No policy registered / policy raises / no lanes -> byte-identical upstream path.
"""
from __future__ import annotations

import types
import pytest

import tools.delegate_tool as _dt


class _FakeParent:
    pass


def _cfg(**kw):
    base = {"model": None, "provider": None, "base_url": None, "api_mode": None}
    base.update(kw)
    return base


@pytest.fixture(autouse=True)
def _clean_registry():
    before = list(_dt._CHILD_WORKER_POLICIES)
    _dt._CHILD_WORKER_POLICIES.clear()
    yield
    _dt._CHILD_WORKER_POLICIES[:] = before


@pytest.fixture
def _fake_runtime(monkeypatch):
    def _install(resolvable):
        captured = {}
        import hermes_cli.runtime_provider as rp_mod
        from types import SimpleNamespace
        real = rp_mod.resolve_runtime_provider
        def fake(requested, target_model):
            captured["requested"] = requested
            if not resolvable:
                raise ValueError("nope")
            return {
                "provider": "custom", "api_key": "k", "base_url": "http://x",
                "api_mode": "chat_completions", "request_overrides": {},
                "max_output_tokens": None, "model": target_model,
            }
        monkeypatch.setattr(rp_mod, "resolve_runtime_provider", fake)
        return captured
    return _install


def test_no_policy_inherits_parent(_clean_registry):
    res = _dt._resolve_delegation_credentials(_cfg(provider=None), _FakeParent())
    assert res["provider"] is None
    assert res["model"] is None


def test_policy_error_tolerated_falls_through(_clean_registry, _fake_runtime):
    def pol(cfg, parent=None):
        raise RuntimeError("boom")
    _dt.register_child_worker_policy(pol)
    res = _dt._resolve_delegation_credentials(_cfg(provider=None), _FakeParent())
    assert res["provider"] is None


def test_policy_ignored_when_no_opinion(_clean_registry, _fake_runtime):
    def pol(cfg, parent=None):
        return None
    _dt.register_child_worker_policy(pol)
    res = _dt._resolve_delegation_credentials(_cfg(provider=None), _FakeParent())
    assert res["provider"] is None


def test_policy_never_receives_credentials_or_live_parent(_clean_registry):
    captured = {}

    def pol(cfg, parent=None):
        captured["cfg"] = cfg
        captured["parent"] = parent
        return None

    parent = _FakeParent()
    parent.api_key = "parent-secret"
    _dt.register_child_worker_policy(pol)
    _dt._resolve_delegation_credentials(
        _cfg(
            provider=None,
            api_key="direct-secret",
            apiKey="camel-secret",
            access_token="access-secret",
            api_secret="api-secret",
            secret_key="secret-key",
            auth="auth-secret",
            authorization="Bearer auth-secret",
            headers={"Authorization": "Bearer nested-secret"},
            nested={"API_KEY": "nested-secret", "lane": "fast"},
        ),
        parent,
    )

    assert captured == {
        "cfg": {
            "model": None,
            "provider": None,
            "base_url": None,
            "api_mode": None,
            "nested": {"lane": "fast"},
        },
        "parent": None,
    }


def test_policy_opinion_is_authoritative(_clean_registry, _fake_runtime):
    def pol(cfg, parent=None):
        return {"provider": "zeus", "model": "qwen3.8-27b", "api_mode": "chat_completions"}
    _dt.register_child_worker_policy(pol)
    captured = _fake_runtime(resolvable=True)
    res = _dt._resolve_delegation_credentials(
        _cfg(provider="openrouter", model="legacy-flat-model"), _FakeParent()
    )
    assert captured["requested"] == "zeus"
    assert res["provider"] in ("zeus", "custom")
    assert res["model"] == "qwen3.8-27b"


def test_policy_unresolvable_provider_fails_cleanly(_clean_registry, _fake_runtime):
    def pol(cfg, parent=None):
        return {"provider": "zeus", "model": "qwen3.8-27b"}
    _dt.register_child_worker_policy(pol)
    _fake_runtime(resolvable=False)
    with pytest.raises(ValueError):
        _dt._resolve_delegation_credentials(_cfg(provider=None), _FakeParent())


def test_flat_provider_still_resolves_direct(_clean_registry, _fake_runtime):
    """Configured flat provider name flows through unchanged (no policy involved)."""
    _fake_runtime(resolvable=True)
    res = _dt._resolve_delegation_credentials(
        _cfg(provider="openrouter", model="m"), _FakeParent())
    assert res["provider"] is not None
