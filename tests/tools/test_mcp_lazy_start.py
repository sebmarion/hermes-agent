"""Behavior-contract tests for lazy MCP server startup (#56832).

A server configured with ``lazy: true`` whose config fingerprint matches an
on-disk schema-cache entry registers its tools WITHOUT spawning/connecting;
the first real call (raw tool OR resource/prompt utility) routes through the
existing connect path.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tools.mcp_tool as mcp


@pytest.fixture(autouse=True)
def _reset_mcp_state():
    with mcp._lock:
        old_servers = dict(mcp._servers)
        old_lazy = dict(mcp._lazy_server_configs)
        old_fps = dict(mcp._lazy_server_fingerprints)
        old_names = dict(mcp._lazy_server_tool_names)
        old_connecting = set(mcp._server_connecting)
        old_tool_servers = dict(mcp._mcp_tool_server_names)
        old_tool_origins = dict(mcp._mcp_tool_server_origins)
        old_server_origins = dict(mcp._mcp_server_origins)
        old_connecting_origins = dict(mcp._mcp_connecting_origins)
        old_registration_identities = dict(
            mcp._mcp_server_registration_identities
        )
    yield
    with mcp._lock:
        for mapping, saved in (
            (mcp._servers, old_servers),
            (mcp._lazy_server_configs, old_lazy),
            (mcp._lazy_server_fingerprints, old_fps),
            (mcp._lazy_server_tool_names, old_names),
            (mcp._mcp_tool_server_names, old_tool_servers),
            (mcp._mcp_tool_server_origins, old_tool_origins),
            (mcp._mcp_server_origins, old_server_origins),
            (mcp._mcp_connecting_origins, old_connecting_origins),
            (
                mcp._mcp_server_registration_identities,
                old_registration_identities,
            ),
        ):
            mapping.clear()
            mapping.update(saved)
        mcp._server_connecting.clear()
        mcp._server_connecting.update(old_connecting)


def _fake_cache_entry():
    return {
        "fingerprint": "abc",
        "tools": [
            {
                "name": "browser_navigate",
                "description": "Navigate",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ],
        "utility_tools": [],
    }


def _lazy_config():
    return {
        "playwright": {
            "command": "npx",
            "args": ["-y", "@playwright/mcp"],
            "lazy": True,
        }
    }


class TestLazyMcpRegistration:
    def test_cached_registration_keeps_config_source_ownership(self):
        config = _lazy_config()
        with (
            patch("tools.mcp_tool._MCP_AVAILABLE", True),
            patch("tools.mcp_schema_cache.config_fingerprint", return_value="abc"),
            patch("tools.mcp_schema_cache.get_cached_entry", return_value=_fake_cache_entry()),
            patch(
                "tools.mcp_tool._register_from_cache_sync",
                return_value=["mcp__playwright__browser_navigate"],
            ) as mock_register,
        ):
            mcp.register_mcp_servers(config, source="config")

        assert mock_register.call_args.kwargs["source"] == "config"
        assert mcp.get_mcp_server_registration_source("playwright") == "config"
        with mcp._lock:
            assert "playwright" not in mcp._mcp_connecting_origins

    def test_reconnect_after_shutdown_recovers_cached_tool_source(self):
        """Cleared reservations must not relabel cached config tools external."""
        config = _lazy_config()["playwright"]
        tool_name = "mcp__playwright__browser_navigate"
        fake_server = SimpleNamespace(
            session=object(),
            _registered_tool_names=[tool_name],
        )
        captured = {}

        with mcp._lock:
            mcp._lazy_server_configs["playwright"] = dict(config)
            mcp._lazy_server_tool_names["playwright"] = [tool_name]
            mcp._mcp_tool_server_names[tool_name] = "playwright"
            mcp._mcp_tool_server_origins[tool_name] = "config"
            mcp._mcp_server_origins.pop("playwright", None)
            mcp._mcp_connecting_origins.pop("playwright", None)

        def fake_run_on_loop(*_args, **_kwargs):
            with mcp._lock:
                captured["source"] = mcp._mcp_connecting_origins["playwright"]
                mcp._mcp_server_origins["playwright"] = (
                    mcp._mcp_connecting_origins.pop("playwright")
                )
                mcp._servers["playwright"] = fake_server
            return []

        with (
            patch("tools.mcp_tool._ensure_mcp_loop"),
            patch(
                "tools.mcp_tool._run_on_mcp_loop",
                side_effect=fake_run_on_loop,
            ),
        ):
            assert mcp._ensure_lazy_server_connected("playwright") is True

        assert captured["source"] == "config"
        assert mcp.get_mcp_server_registration_source("playwright") == "config"

    def test_full_shutdown_clears_lazy_registration_for_source_transfer(self):
        from tools.registry import registry

        tool_name = "mcp__playwright__browser_navigate"
        registry.register(
            name=tool_name,
            toolset="mcp-playwright",
            schema={
                "name": tool_name,
                "description": "Navigate",
                "parameters": {"type": "object", "properties": {}},
            },
            handler=lambda *_args, **_kwargs: "{}",
        )
        registry.register_toolset_alias("playwright", "mcp-playwright")
        with mcp._lock:
            mcp._servers.clear()
            mcp._lazy_server_configs["playwright"] = _lazy_config()["playwright"]
            mcp._lazy_server_fingerprints["playwright"] = "abc"
            mcp._lazy_server_tool_names["playwright"] = [tool_name]
            mcp._mcp_tool_server_names[tool_name] = "playwright"
            mcp._mcp_tool_server_origins[tool_name] = "config"
            mcp._mcp_server_origins["playwright"] = "config"
            mcp._mcp_server_registration_identities["playwright"] = "old"

        try:
            with patch("tools.mcp_tool._stop_mcp_loop"):
                mcp.shutdown_mcp_servers()

            with mcp._lock:
                assert "playwright" not in mcp._lazy_server_configs
                assert "playwright" not in mcp._lazy_server_fingerprints
                assert "playwright" not in mcp._lazy_server_tool_names
                assert tool_name not in mcp._mcp_tool_server_names
                assert tool_name not in mcp._mcp_tool_server_origins
                assert "playwright" not in mcp._mcp_server_registration_identities
            assert tool_name not in registry.get_all_tool_names()

            with (
                patch("tools.mcp_tool._MCP_AVAILABLE", True),
                patch(
                    "tools.mcp_tool._filter_suspicious_mcp_servers",
                    side_effect=lambda servers: servers,
                ),
                patch("tools.mcp_tool._ensure_mcp_loop"),
                patch("tools.mcp_tool._run_on_mcp_loop") as mock_run,
            ):
                mcp.register_mcp_servers(
                    {"playwright": {"command": "new-acp-server"}},
                    source="acp",
                )

            mock_run.assert_called_once()
            assert mcp.get_mcp_server_registration_source("playwright") == "acp"
        finally:
            registry.deregister(tool_name)

    def test_registers_from_cache_without_connect(self):
        config = _lazy_config()
        with patch("tools.mcp_tool._MCP_AVAILABLE", True), \
             patch("tools.mcp_schema_cache.config_fingerprint", return_value="abc"), \
             patch("tools.mcp_schema_cache.get_cached_entry", return_value=_fake_cache_entry()), \
             patch(
                 "tools.mcp_tool._register_from_cache_sync",
                 return_value=["mcp_playwright_browser_navigate"],
             ) as mock_register, \
             patch("tools.mcp_tool._discover_and_register_server", new_callable=AsyncMock) as mock_discover, \
             patch("tools.mcp_tool._ensure_mcp_loop") as mock_loop, \
             patch("tools.mcp_tool._run_on_mcp_loop") as mock_run:

            mcp.register_mcp_servers(config)

        mock_register.assert_called_once()
        mock_discover.assert_not_called()
        mock_run.assert_not_called()
        mock_loop.assert_not_called()

    def test_cache_miss_falls_back_to_eager_connect(self):
        config = _lazy_config()
        with patch("tools.mcp_tool._MCP_AVAILABLE", True), \
             patch("tools.mcp_schema_cache.config_fingerprint", return_value="abc"), \
             patch("tools.mcp_schema_cache.get_cached_entry", return_value=None), \
             patch("tools.mcp_tool._ensure_mcp_loop"), \
             patch("tools.mcp_tool._run_on_mcp_loop") as mock_run:

            mcp.register_mcp_servers(config)

        mock_run.assert_called_once()

    def test_non_lazy_server_never_touches_cache(self):
        config = {"playwright": {"command": "npx", "args": []}}
        with patch("tools.mcp_tool._MCP_AVAILABLE", True), \
             patch("tools.mcp_schema_cache.get_cached_entry") as mock_get, \
             patch("tools.mcp_tool._ensure_mcp_loop"), \
             patch("tools.mcp_tool._run_on_mcp_loop") as mock_run:

            mcp.register_mcp_servers(config)

        mock_get.assert_not_called()
        mock_run.assert_called_once()

    def test_lazy_server_not_reregistered_on_second_pass(self):
        config = _lazy_config()
        mcp._lazy_server_configs["playwright"] = dict(config["playwright"])
        mcp._lazy_server_tool_names["playwright"] = ["mcp_playwright_browser_navigate"]
        with patch("tools.mcp_tool._MCP_AVAILABLE", True), \
             patch("tools.mcp_tool._register_from_cache_sync") as mock_register, \
             patch("tools.mcp_tool._run_on_mcp_loop") as mock_run:

            names = mcp.register_mcp_servers(config)

        mock_register.assert_not_called()
        mock_run.assert_not_called()
        assert "mcp_playwright_browser_navigate" in names


class TestLazyMCPRawToolsetAliasPublication:
    @pytest.mark.parametrize(
        ("server_name", "collision_kind"),
        [
            ("collision-lazy-alias", "alias"),
            ("collision-lazy-canonical", "canonical"),
            ("web", "static"),
        ],
    )
    def test_cached_registration_preserves_non_mcp_raw_toolset(
        self, server_name, collision_kind
    ):
        from tools.registry import ToolRegistry
        from toolsets import resolve_toolset

        registry = ToolRegistry()
        incumbent_tool = f"incumbent_{collision_kind}_tool"
        incumbent_alias_target = None

        if collision_kind == "alias":
            incumbent_alias_target = f"plugin-{server_name}"
            registry.register(
                name=incumbent_tool,
                toolset=incumbent_alias_target,
                schema={
                    "name": incumbent_tool,
                    "description": "Incumbent plugin tool",
                    "parameters": {"type": "object", "properties": {}},
                },
                handler=lambda _args, **_kwargs: "{}",
            )
            registry.register_toolset_alias(server_name, incumbent_alias_target)
        elif collision_kind == "canonical":
            registry.register(
                name=incumbent_tool,
                toolset=server_name,
                schema={
                    "name": incumbent_tool,
                    "description": "Incumbent canonical tool",
                    "parameters": {"type": "object", "properties": {}},
                },
                handler=lambda _args, **_kwargs: "{}",
            )

        entry = {
            "tools": [
                {
                    "name": "remote",
                    "description": "Remote MCP tool",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ],
            "utility_tools": [],
        }

        with patch("tools.registry.registry", registry):
            registered = mcp._register_from_cache_sync(
                server_name,
                {"command": "fake"},
                entry,
                source="config",
            )
            raw_tools = resolve_toolset(server_name)
            mcp_tools = resolve_toolset(f"mcp-{server_name}")

        assert len(registered) == 1
        mcp_tool = registered[0]
        assert registry.get_toolset_alias_target(server_name) == (
            incumbent_alias_target
        )
        assert mcp_tool not in raw_tools
        assert mcp_tools == [mcp_tool]
        if collision_kind == "static":
            assert "web_search" in raw_tools
        else:
            assert incumbent_tool in raw_tools


class TestLazyFirstUseConnect:
    def _connected_server(self):
        mock_session = MagicMock()
        mock_session.call_tool = AsyncMock(
            return_value=SimpleNamespace(isError=False, content=[], structuredContent=None)
        )
        connected = SimpleNamespace(
            session=mock_session,
            _rpc_lock=MagicMock(),
            _pending_call_context=None,
        )
        connected._rpc_lock.__aenter__ = AsyncMock(return_value=None)
        connected._rpc_lock.__aexit__ = AsyncMock(return_value=None)
        return connected

    @staticmethod
    def _run_on_loop(coro_or_factory, timeout=120):
        import asyncio

        coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_tool_handler_lazy_connects_on_first_call(self):
        config = {"command": "npx", "args": [], "lazy": True, "timeout": 5}
        mcp._lazy_server_configs["playwright"] = dict(config)
        mcp._lazy_server_fingerprints["playwright"] = "abc"

        connected = self._connected_server()

        def _connect(name):
            mcp._servers["playwright"] = connected
            return True

        with patch.object(mcp, "_ensure_lazy_server_connected", side_effect=_connect) as mock_connect, \
             patch.object(mcp, "_run_on_mcp_loop", side_effect=self._run_on_loop):
            handler = mcp._make_tool_handler("playwright", "browser_navigate", 5)
            out = handler({}, task_id="t1")

        mock_connect.assert_called_once_with("playwright")
        payload = json.loads(out)
        assert "error" not in payload
        assert payload.get("result") == ""

    def test_list_resources_handler_lazy_connects_on_first_call(self):
        # Regression for the resource/prompt gap: utility handlers must also
        # route through the first-use connect path, or the first
        # list_resources/get_prompt on a lazy server fails.
        config = {"command": "npx", "args": [], "lazy": True, "timeout": 5}
        mcp._lazy_server_configs["playwright"] = dict(config)

        connected = self._connected_server()
        connected.session.list_resources = AsyncMock()

        def _connect(name):
            mcp._servers["playwright"] = connected
            return True

        async def _fake_paginate(list_method, items_attr, server_name):
            return [SimpleNamespace(uri="file:///a", name="a", description="", mimeType="")]

        with patch.object(mcp, "_ensure_lazy_server_connected", side_effect=_connect) as mock_connect, \
             patch.object(mcp, "_paginate_full_list", side_effect=_fake_paginate), \
             patch.object(mcp, "_run_on_mcp_loop", side_effect=self._run_on_loop):
            handler = mcp._make_list_resources_handler("playwright", 5)
            out = handler({})

        mock_connect.assert_called_once_with("playwright")
        payload = json.loads(out)
        assert "error" not in payload
        assert payload["resources"][0]["uri"] == "file:///a"

    def test_get_prompt_handler_lazy_connects_on_first_call(self):
        config = {"command": "npx", "args": [], "lazy": True, "timeout": 5}
        mcp._lazy_server_configs["playwright"] = dict(config)

        connected = self._connected_server()
        connected.session.get_prompt = AsyncMock(
            return_value=SimpleNamespace(messages=[])
        )

        def _connect(name):
            mcp._servers["playwright"] = connected
            return True

        with patch.object(mcp, "_ensure_lazy_server_connected", side_effect=_connect) as mock_connect, \
             patch.object(mcp, "_run_on_mcp_loop", side_effect=self._run_on_loop):
            handler = mcp._make_get_prompt_handler("playwright", 5)
            out = handler({"name": "greeting"})

        mock_connect.assert_called_once_with("playwright")
        payload = json.loads(out)
        assert "error" not in payload

    def test_check_fn_passes_for_lazy_registered_server(self):
        mcp._lazy_server_configs["playwright"] = {"lazy": True}
        mcp._lazy_server_fingerprints["playwright"] = "abc"
        assert mcp._make_check_fn("playwright")() is True

    def test_check_fn_fails_for_unknown_server(self):
        assert mcp._make_check_fn("nope")() is False

    def test_lazy_connect_respects_connect_cooldown(self):
        mcp._lazy_server_configs["playwright"] = {"command": "npx", "lazy": True}
        with patch.object(mcp, "_connect_cooldown_active", return_value=True), \
             patch.object(mcp, "_run_on_mcp_loop") as mock_run:
            assert mcp._ensure_lazy_server_connected("playwright") is False
        mock_run.assert_not_called()

    def test_lazy_connect_success_clears_lazy_state(self):
        config = {"command": "npx", "lazy": True}
        mcp._lazy_server_configs["playwright"] = dict(config)
        mcp._lazy_server_fingerprints["playwright"] = "abc"
        mcp._lazy_server_tool_names["playwright"] = ["mcp_playwright_browser_navigate"]

        connected = SimpleNamespace(
            session=MagicMock(),
            _registered_tool_names=["mcp_playwright_browser_navigate"],
        )

        def _fake_run(coro_or_factory, timeout=30):
            mcp._servers["playwright"] = connected
            coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
            coro.close()
            return ["mcp_playwright_browser_navigate"]

        with patch.object(mcp, "_ensure_mcp_loop"), \
             patch.object(mcp, "_run_on_mcp_loop", side_effect=_fake_run):
            assert mcp._ensure_lazy_server_connected("playwright") is True

        assert "playwright" not in mcp._lazy_server_configs
        assert "playwright" not in mcp._lazy_server_fingerprints
        assert "playwright" not in mcp._lazy_server_tool_names

    def test_lazy_connect_deregisters_phantom_cached_tools(self):
        # Stale-cache reconciliation: the cached manifest advertised tool X,
        # but the live server only registers tool Y → X must be deregistered
        # after the first-use connect so the model stops seeing a phantom.
        from tools.registry import registry

        mcp._lazy_server_configs["playwright"] = {"command": "npx", "lazy": True}
        mcp._lazy_server_fingerprints["playwright"] = "stale-fp"
        mcp._lazy_server_tool_names["playwright"] = [
            "mcp_playwright_tool_x",
            "mcp_playwright_tool_y",
        ]

        connected = SimpleNamespace(
            session=MagicMock(),
            _registered_tool_names=["mcp_playwright_tool_y"],
        )

        def _fake_run(coro_or_factory, timeout=30):
            mcp._servers["playwright"] = connected
            coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
            coro.close()
            return ["mcp_playwright_tool_y"]

        with patch.object(mcp, "_ensure_mcp_loop"), \
             patch.object(mcp, "_run_on_mcp_loop", side_effect=_fake_run), \
             patch.object(registry, "deregister") as mock_dereg:
            assert mcp._ensure_lazy_server_connected("playwright") is True

        mock_dereg.assert_called_once_with("mcp_playwright_tool_x")

    def test_lazy_connect_failure_records_cooldown(self):
        mcp._lazy_server_configs["playwright"] = {"command": "npx", "lazy": True}

        def _fake_run(coro_or_factory, timeout=30):
            coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
            coro.close()
            raise RuntimeError("spawn failed")

        with patch.object(mcp, "_ensure_mcp_loop"), \
             patch.object(mcp, "_run_on_mcp_loop", side_effect=_fake_run), \
             patch.object(mcp, "_record_connect_failure") as mock_record:
            assert mcp._ensure_lazy_server_connected("playwright") is False

        mock_record.assert_called_once_with("playwright")
        # Config retained so a later call can retry after cooldown.
        assert "playwright" in mcp._lazy_server_configs


class TestCacheLoadDescriptionScan:
    def test_scan_runs_on_cache_load_path(self):
        # Defense-in-depth: the cache file is user-writable JSON, so the
        # cache-load registration path must run the same injection scan as
        # eager discovery.
        entry = _fake_cache_entry()
        config = {"command": "npx", "args": [], "lazy": True}
        with patch.object(mcp, "_scan_mcp_description", return_value=[]) as mock_scan, \
             patch.object(mcp, "_convert_mcp_schema", side_effect=RuntimeError("stop")), \
             pytest.raises(RuntimeError):
            mcp._register_from_cache_sync("playwright", config, entry)

        mock_scan.assert_called_once_with("playwright", "browser_navigate", "Navigate")


class TestResolveServerLazy:
    def test_default_off(self):
        assert mcp._resolve_server_lazy("s", {"command": "npx"}) is False

    def test_explicit_true(self):
        assert mcp._resolve_server_lazy("s", {"command": "npx", "lazy": True}) is True

    def test_explicit_false(self):
        assert mcp._resolve_server_lazy("s", {"command": "npx", "lazy": False}) is False
