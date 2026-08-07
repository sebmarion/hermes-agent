"""Tests for model_tools.py — function call dispatch, agent-loop interception, legacy toolsets."""

import builtins
import json
from types import SimpleNamespace
from unittest.mock import ANY, call, patch


from model_tools import (
    handle_function_call,
    get_tool_definitions,
    get_all_tool_names,
    get_toolset_for_tool,
    _AGENT_LOOP_TOOLS,
    _LEGACY_TOOLSET_MAP,
    TOOL_TO_TOOLSET_MAP,
)


# =========================================================================
# handle_function_call
# =========================================================================


def test_tool_definition_assembly_filters_mcp_before_tool_search(monkeypatch):
    import tools.mcp_tool as mcp_tool

    native = {
        "type": "function",
        "function": {"name": "terminal", "description": "", "parameters": {}},
    }
    remote = {
        "type": "function",
        "function": {"name": "mcp__zeus__open", "description": "", "parameters": {}},
    }
    with mcp_tool._lock:
        mcp_tool._mcp_tool_server_names["mcp__zeus__open"] = "zeus"
        mcp_tool._mcp_tool_server_origins["mcp__zeus__open"] = "config"
    monkeypatch.setattr(
        mcp_tool,
        "_load_mcp_config",
        lambda: {"zeus": {"allowed_platforms": ["cli"]}},
    )
    monkeypatch.setattr("model_tools.registry.get_definitions", lambda *_args, **_kwargs: [native, remote])

    try:
        result = get_tool_definitions(
            quiet_mode=False,
            skip_tool_search_assembly=True,
            platform="telegram",
        )
    finally:
        with mcp_tool._lock:
            mcp_tool._mcp_tool_server_names.pop("mcp__zeus__open", None)
            mcp_tool._mcp_tool_server_origins.pop("mcp__zeus__open", None)

    assert [tool["function"]["name"] for tool in result] == ["terminal"]


def test_tool_definition_policy_import_failure_keeps_native_and_drops_mcp(
    monkeypatch,
):
    native = {
        "type": "function",
        "function": {"name": "terminal", "description": "", "parameters": {}},
    }
    remote = {
        "type": "function",
        "function": {
            "name": "mcp__zeus__open",
            "description": "",
            "parameters": {},
        },
    }
    real_import = builtins.__import__

    def fail_policy_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "tools.mcp_tool":
            raise ImportError("policy unavailable")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(
        "model_tools.registry.get_definitions",
        lambda *_args, **_kwargs: [native, remote],
    )
    monkeypatch.setattr(builtins, "__import__", fail_policy_import)

    result = get_tool_definitions(
        quiet_mode=False,
        skip_tool_search_assembly=True,
        platform="cli",
    )

    assert [tool["function"]["name"] for tool in result] == ["terminal"]

class TestHandleFunctionCall:
    def test_agent_loop_tool_returns_error(self):
        for tool_name in _AGENT_LOOP_TOOLS:
            result = json.loads(handle_function_call(tool_name, {}))
            assert "error" in result
            assert "agent loop" in result["error"].lower()

    def test_unknown_tool_returns_error(self):
        result = json.loads(handle_function_call("totally_fake_tool_xyz", {}))
        assert "error" in result
        assert "totally_fake_tool_xyz" in result["error"]

    def test_platform_is_forwarded_to_registry_dispatch(self, monkeypatch):
        seen = {}

        def fake_dispatch(name, args, **kwargs):
            seen.update({"name": name, "args": args, "platform": kwargs.get("platform")})
            return '{"ok":true}'

        monkeypatch.setattr("model_tools.registry.dispatch", fake_dispatch)

        result = handle_function_call(
            "web_search", {"q": "test"}, task_id="platform-test", platform="cli"
        )

        assert result == '{"ok":true}'
        assert seen == {"name": "web_search", "args": {"q": "test"}, "platform": "cli"}

    def test_tool_call_bridge_preserves_platform_for_underlying_dispatch(self, monkeypatch):
        from tools import tool_search

        seen = {}
        monkeypatch.setattr(
            "model_tools.get_tool_definitions",
            lambda **kwargs: [{
                "type": "function",
                "function": {"name": "mcp__zeus__open", "description": "", "parameters": {}},
            }],
        )
        monkeypatch.setattr(
            tool_search,
            "resolve_underlying_call",
            lambda _args: ("mcp__zeus__open", {"url": "https://example.com"}, None),
        )
        monkeypatch.setattr(
            tool_search,
            "scoped_deferrable_names",
            lambda _defs: frozenset({"mcp__zeus__open"}),
        )
        monkeypatch.setattr(
            "model_tools.registry.dispatch",
            lambda name, args, **kwargs: seen.update(
                {"name": name, "args": args, "platform": kwargs.get("platform")}
            ) or '{"ok":true}',
        )

        result = handle_function_call(
            "tool_call",
            {"name": "mcp__zeus__open", "arguments": {"url": "https://example.com"}},
            platform="cli",
        )

        assert result == '{"ok":true}'
        assert seen == {
            "name": "mcp__zeus__open",
            "args": {"url": "https://example.com"},
            "platform": "cli",
        }



    def test_post_tool_call_receives_non_negative_integer_duration_ms(self):
        """Regression: post_tool_call and transform_tool_result hooks must
        receive a non-negative integer ``duration_ms`` kwarg measuring
        dispatch latency.  Inspired by Claude Code 2.1.119, which added
        ``duration_ms`` to its PostToolUse hook inputs.
        """
        with (
            patch("model_tools.registry.dispatch", return_value='{"ok":true}'),
            patch("hermes_cli.plugins.has_hook", return_value=True),
            patch("hermes_cli.plugins.invoke_hook") as mock_invoke_hook,
        ):
            handle_function_call("web_search", {"q": "test"}, task_id="t1")

        kwargs_by_hook = {
            c.args[0]: c.kwargs for c in mock_invoke_hook.call_args_list
        }
        assert "duration_ms" in kwargs_by_hook["post_tool_call"]
        assert "duration_ms" in kwargs_by_hook["transform_tool_result"]

        post_duration = kwargs_by_hook["post_tool_call"]["duration_ms"]
        transform_duration = kwargs_by_hook["transform_tool_result"]["duration_ms"]
        assert isinstance(post_duration, int)
        assert post_duration >= 0
        # Both hooks should observe the same measured duration.
        assert post_duration == transform_duration
        # pre_tool_call does NOT get duration_ms (nothing has run yet).
        assert "duration_ms" not in kwargs_by_hook["pre_tool_call"]

    def test_terminal_nonzero_exit_is_reported_as_error(self):
        result = json.dumps({"output": "", "exit_code": 1, "error": None})
        with (
            patch("model_tools.registry.dispatch", return_value=result),
            patch("hermes_cli.plugins.has_hook", return_value=True),
            patch("hermes_cli.plugins.invoke_hook") as mock_invoke_hook,
        ):
            assert handle_function_call("terminal", {"command": "false"}) == result

        kwargs_by_hook = {
            hook.args[0]: hook.kwargs for hook in mock_invoke_hook.call_args_list
        }
        for hook_name in ("post_tool_call", "transform_tool_result"):
            assert kwargs_by_hook[hook_name]["status"] == "error"
            assert kwargs_by_hook[hook_name]["error_type"] == "tool_error"
            assert kwargs_by_hook[hook_name]["error_message"] == "exit 1"

    def test_no_listener_skips_post_and_transform_emit(self):
        """When no plugin is registered for post_tool_call /
        transform_tool_result, the emit path must short-circuit on
        ``has_hook`` and never build/dispatch a payload — so the
        no-listener hot path stays cheap.  ``pre_tool_call`` is always
        polled (block-check), so it may still fire; the observer/transform
        emits must not.
        """
        with (
            patch("model_tools.registry.dispatch", return_value='{"ok":true}'),
            patch("hermes_cli.plugins.has_hook", return_value=False),
            patch("hermes_cli.plugins.invoke_hook") as mock_invoke_hook,
        ):
            result = handle_function_call("web_search", {"q": "test"}, task_id="t1")

        assert result == '{"ok":true}'
        fired = {c.args[0] for c in mock_invoke_hook.call_args_list}
        assert "post_tool_call" not in fired
        assert "transform_tool_result" not in fired

    def test_tool_request_and_execution_middleware_wrap_registry_dispatch(self, monkeypatch):
        seen = {}

        def fake_invoke_middleware(kind, **kwargs):
            if kind == "tool_request":
                return [{
                    "args": {**kwargs["args"], "rewritten": True},
                    "source": "test-middleware",
                    "reason": "rewrite",
                }]
            return []

        def execution_middleware(**kwargs):
            seen["execution_args"] = kwargs["args"]
            return kwargs["next_call"]({**kwargs["args"], "wrapped": True})

        def fake_dispatch(tool_name, args, **kwargs):
            seen["dispatch"] = (tool_name, args, kwargs)
            return json.dumps({"ok": True, "args": args})

        manager = type(
            "Manager",
            (),
            {"_middleware": {"tool_request": [fake_invoke_middleware], "tool_execution": [execution_middleware]}},
        )()
        monkeypatch.setattr("hermes_cli.plugins.invoke_middleware", fake_invoke_middleware)
        monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
        hook_calls = []
        monkeypatch.setattr(
            "hermes_cli.plugins.invoke_hook",
            lambda hook_name, **kwargs: hook_calls.append((hook_name, kwargs)) or [],
        )
        monkeypatch.setattr("hermes_cli.plugins.has_hook", lambda name: True)
        monkeypatch.setattr("model_tools.registry.dispatch", fake_dispatch)

        result = json.loads(
            handle_function_call(
                "web_search",
                {"q": "test"},
                task_id="task-1",
                tool_call_id="tool-1",
                session_id="session-1",
            )
        )

        assert seen["execution_args"] == {"q": "test", "rewritten": True}
        assert seen["dispatch"][1] == {"q": "test", "rewritten": True, "wrapped": True}
        assert result["args"] == {"q": "test", "rewritten": True, "wrapped": True}
        expected_trace = [{"source": "test-middleware", "reason": "rewrite"}]
        pre_call = next(call for call in hook_calls if call[0] == "pre_tool_call")
        post_call = next(call for call in hook_calls if call[0] == "post_tool_call")
        assert pre_call[1]["middleware_trace"] == expected_trace
        assert post_call[1]["middleware_trace"] == expected_trace

    def test_registry_exception_emits_terminal_tool_hook(self, monkeypatch):
        from hermes_cli import lifecycle

        hook_calls = []
        monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])
        monkeypatch.setattr(lifecycle, "has_hook", lambda name: name == "post_tool_call")
        monkeypatch.setattr(
            lifecycle,
            "invoke_hook",
            lambda name, **kwargs: hook_calls.append((name, kwargs)) or [],
        )
        monkeypatch.setattr(
            "model_tools.registry.dispatch",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        result = json.loads(
            handle_function_call(
                "web_search",
                {"q": "test"},
                task_id="task-1",
                session_id="session-1",
                tool_call_id="tool-1",
            )
        )

        assert "error" in result
        [post_call] = [call for call in hook_calls if call[0] == "post_tool_call"]
        assert post_call[1]["status"] == "error"
        assert post_call[1]["error_type"] == "RuntimeError"
        assert post_call[1]["duration_ms"] >= 0

    def test_acp_edit_denial_emits_blocked_terminal_tool_hook(self, monkeypatch):
        from hermes_cli import lifecycle

        hook_calls = []
        monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])
        monkeypatch.setattr(lifecycle, "has_hook", lambda name: name == "post_tool_call")
        monkeypatch.setattr(
            lifecycle,
            "invoke_hook",
            lambda name, **kwargs: hook_calls.append((name, kwargs)) or [],
        )
        monkeypatch.setattr(
            "acp_adapter.edit_approval.maybe_require_edit_approval",
            lambda *_args, **_kwargs: json.dumps({"error": "Edit approval denied"}),
        )
        monkeypatch.setattr(
            "model_tools.registry.dispatch",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("denied edit must not dispatch")
            ),
        )

        result = json.loads(
            handle_function_call(
                "write_file",
                {"path": "private.txt", "content": "private"},
                task_id="task-1",
                session_id="session-1",
                tool_call_id="tool-1",
            )
        )

        assert result == {"error": "Edit approval denied"}
        [post_call] = [call for call in hook_calls if call[0] == "post_tool_call"]
        assert post_call[1]["status"] == "blocked"
        assert post_call[1]["error_type"] == "edit_approval_denied"


class TestRequiredPolicyFinalDispatch:
    def test_handle_function_call_carries_original_final_args_and_identity(
        self,
        monkeypatch,
        tmp_path,
    ):
        from hermes_cli.tool_policy import ToolDispatchPolicyInput

        monkeypatch.chdir(tmp_path)
        policy_inputs: list[ToolDispatchPolicyInput] = []
        dispatch_calls: list[tuple[str, dict, dict]] = []

        def request_middleware(**kwargs):
            return {
                "args": {**kwargs["args"], "stage": "request"},
                "source": "request-test",
            }

        def execution_middleware(**kwargs):
            return kwargs["next_call"](
                {**kwargs["args"], "stage": "execution"}
            )

        manager = SimpleNamespace(
            _middleware={
                "tool_request": [request_middleware],
                "tool_execution": [execution_middleware],
            }
        )
        monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
        monkeypatch.setattr(
            "hermes_cli.plugins.invoke_middleware",
            lambda kind, **kwargs: (
                [request_middleware(**kwargs)] if kind == "tool_request" else []
            ),
        )
        monkeypatch.setattr(
            "hermes_cli.plugins.get_pre_tool_call_block_message",
            lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(
            "hermes_cli.plugins.authorize_required_tool_policies",
            lambda policy_input: policy_inputs.append(policy_input),
        )
        monkeypatch.setattr("hermes_cli.plugins.has_hook", lambda _name: False)
        monkeypatch.setattr(
            "model_tools.registry.dispatch",
            lambda name, args, **kwargs: (
                dispatch_calls.append((name, args, kwargs)) or '{"ok":true}'
            ),
        )

        result = handle_function_call(
            "web_search",
            {"q": "test", "stage": "original"},
            task_id="task-1",
            session_id="session-1",
            turn_id="turn-1",
            tool_call_id="call-1",
            api_request_id="request-1",
        )

        assert result == '{"ok":true}'
        assert len(policy_inputs) == 1
        assert policy_inputs[0].original_args == {
            "q": "test",
            "stage": "original",
        }
        assert policy_inputs[0].effective_args == {
            "q": "test",
            "stage": "execution",
        }
        assert policy_inputs[0].task_id == "task-1"
        assert policy_inputs[0].session_id == "session-1"
        assert policy_inputs[0].turn_id == "turn-1"
        assert policy_inputs[0].tool_call_id == "call-1"
        assert dispatch_calls == [
            (
                "web_search",
                {"q": "test", "stage": "execution"},
                {
                    "task_id": "task-1",
                    "session_id": "session-1",
                    "user_task": None,
                    "turn_id": "turn-1",
                    "tool_call_id": "call-1",
                },
            )
        ]

    def test_policy_block_precedes_edit_approval_and_emits_once(
        self,
        monkeypatch,
    ):
        from hermes_cli.tool_policy import PolicyDecisionCode, ToolPolicyBlock

        manager = SimpleNamespace(_middleware={})
        block = ToolPolicyBlock(
            policy="tool_dispatch",
            policy_code=PolicyDecisionCode.BLOCKED,
            message="Denied by governor.",
        )
        observer_calls: list[dict] = []
        monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
        monkeypatch.setattr(
            "hermes_cli.plugins.get_pre_tool_call_block_message",
            lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(
            "hermes_cli.plugins.authorize_required_tool_policies",
            lambda _policy_input: block,
        )
        monkeypatch.setattr(
            "acp_adapter.edit_approval.maybe_require_edit_approval",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("edit approval must not run")
            ),
        )
        monkeypatch.setattr(
            "model_tools.registry.dispatch",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("registry handler must not run")
            ),
        )
        monkeypatch.setattr(
            "model_tools._emit_post_tool_call_hook",
            lambda **kwargs: observer_calls.append(kwargs),
        )

        result = handle_function_call(
            "write_file",
            {"path": "x.txt", "content": "x"},
            task_id="task-1",
            session_id="session-1",
            turn_id="turn-1",
            tool_call_id="call-1",
        )

        assert json.loads(result) == block.to_result()
        assert len(observer_calls) == 1
        assert observer_calls[0]["status"] == "blocked"
        assert observer_calls[0]["error_type"] == "required_policy_block"
        assert observer_calls[0]["function_args"] == {
            "path": "x.txt",
            "content": "x",
        }

    def test_execute_code_policy_block_prevents_registry_and_sandbox_start(
        self,
        monkeypatch,
    ):
        from hermes_cli.tool_policy import PolicyDecisionCode, ToolPolicyBlock

        manager = SimpleNamespace(_middleware={})
        block = ToolPolicyBlock(
            policy="tool_dispatch",
            policy_code=PolicyDecisionCode.BLOCKED,
            message="Sandbox denied.",
        )
        monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
        monkeypatch.setattr(
            "hermes_cli.plugins.get_pre_tool_call_block_message",
            lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(
            "hermes_cli.plugins.authorize_required_tool_policies",
            lambda _policy_input: block,
        )
        monkeypatch.setattr(
            "model_tools.registry.dispatch",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("execute_code sandbox must not start")
            ),
        )
        monkeypatch.setattr("model_tools._emit_post_tool_call_hook", lambda **_kwargs: None)

        result = handle_function_call(
            "execute_code",
            {"code": "print('unsafe')"},
            task_id="task-1",
            session_id="session-1",
            turn_id="turn-1",
            tool_call_id="call-1",
        )

        assert json.loads(result) == block.to_result()

    def test_tool_search_unwrap_preserves_underlying_original_and_final_args(
        self,
        monkeypatch,
    ):
        from tools import tool_search

        policy_inputs = []
        dispatch_calls = []

        def request_middleware(**kwargs):
            return {
                "args": {**kwargs["args"], "stage": "request"},
                "source": "request-test",
            }

        def execution_middleware(**kwargs):
            return kwargs["next_call"](
                {**kwargs["args"], "stage": "execution"}
            )

        manager = SimpleNamespace(
            _middleware={
                "tool_request": [request_middleware],
                "tool_execution": [execution_middleware],
            }
        )
        monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
        monkeypatch.setattr(
            "hermes_cli.plugins.invoke_middleware",
            lambda kind, **kwargs: (
                [request_middleware(**kwargs)] if kind == "tool_request" else []
            ),
        )
        monkeypatch.setattr(
            "hermes_cli.plugins.get_pre_tool_call_block_message",
            lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(
            "hermes_cli.plugins.authorize_required_tool_policies",
            lambda policy_input: policy_inputs.append(policy_input),
        )
        monkeypatch.setattr("hermes_cli.plugins.has_hook", lambda _name: False)
        monkeypatch.setattr("model_tools.get_tool_definitions", lambda **_kwargs: [])
        monkeypatch.setattr(
            tool_search,
            "resolve_underlying_call",
            lambda _args: (
                "web_search",
                {"q": "test", "stage": "original"},
                None,
            ),
        )
        monkeypatch.setattr(
            tool_search,
            "scoped_deferrable_names",
            lambda _defs: frozenset({"web_search"}),
        )
        monkeypatch.setattr(
            "model_tools.registry.dispatch",
            lambda name, args, **_kwargs: (
                dispatch_calls.append((name, args)) or '{"ok":true}'
            ),
        )

        result = handle_function_call(
            "tool_call",
            {
                "name": "web_search",
                "arguments": {"q": "test", "stage": "original"},
            },
            task_id="task-1",
            session_id="session-1",
            turn_id="turn-1",
            tool_call_id="call-1",
        )

        assert result == '{"ok":true}'
        assert len(policy_inputs) == 1
        assert policy_inputs[0].tool_name == "web_search"
        assert policy_inputs[0].original_args == {
            "q": "test",
            "stage": "original",
        }
        assert policy_inputs[0].effective_args == {
            "q": "test",
            "stage": "execution",
        }
        assert policy_inputs[0].tool_call_id == "call-1"
        assert dispatch_calls == [
            ("web_search", {"q": "test", "stage": "execution"})
        ]


# =========================================================================
# Agent loop tools
# =========================================================================

class TestAgentLoopTools:
    def test_expected_tools_in_set(self):
        assert "todo" in _AGENT_LOOP_TOOLS
        assert "memory" in _AGENT_LOOP_TOOLS
        assert "session_search" in _AGENT_LOOP_TOOLS
        assert "delegate_task" in _AGENT_LOOP_TOOLS

    def test_no_regular_tools_in_set(self):
        assert "web_search" not in _AGENT_LOOP_TOOLS
        assert "terminal" not in _AGENT_LOOP_TOOLS


# =========================================================================
# Pre-tool-call blocking via plugin hooks
# =========================================================================

class TestPreToolCallBlocking:
    """Verify that pre_tool_call hooks can block tool execution."""

    def test_blocked_tool_returns_error_and_skips_dispatch(self, monkeypatch):
        hook_calls = []

        def fake_invoke_hook(hook_name, **kwargs):
            hook_calls.append((hook_name, kwargs))
            if hook_name == "pre_tool_call":
                return [{"action": "block", "message": "Blocked by policy"}]
            return []

        dispatch_called = False
        _orig_dispatch = None

        def fake_dispatch(*args, **kwargs):
            nonlocal dispatch_called
            dispatch_called = True
            raise AssertionError("dispatch should not run when blocked")

        monkeypatch.setattr("hermes_cli.plugins.invoke_hook", fake_invoke_hook)
        monkeypatch.setattr("hermes_cli.plugins.has_hook", lambda name: True)
        monkeypatch.setattr("model_tools.registry.dispatch", fake_dispatch)

        result = json.loads(handle_function_call("read_file", {"path": "test.txt"}, task_id="t1"))
        assert result == {"error": "Blocked by policy"}
        assert not dispatch_called
        post_call = next(call for call in hook_calls if call[0] == "post_tool_call")
        assert post_call[1]["status"] == "blocked"
        assert post_call[1]["error_type"] == "plugin_block"
        assert post_call[1]["error_message"] == "Blocked by policy"
        assert post_call[1]["duration_ms"] == 0

    def test_blocked_tool_skips_read_loop_notification(self, monkeypatch):
        notifications = []

        def fake_invoke_hook(hook_name, **kwargs):
            if hook_name == "pre_tool_call":
                return [{"action": "block", "message": "Blocked"}]
            return []

        monkeypatch.setattr("hermes_cli.plugins.invoke_hook", fake_invoke_hook)
        monkeypatch.setattr("model_tools.registry.dispatch",
                            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not run")))
        monkeypatch.setattr("tools.file_tools.notify_other_tool_call",
                            lambda task_id: notifications.append(task_id))

        result = json.loads(handle_function_call("web_search", {"q": "test"}, task_id="t1"))
        assert result == {"error": "Blocked"}
        assert notifications == []

    def test_invalid_hook_returns_do_not_block(self, monkeypatch):
        """Malformed hook returns should be ignored — tool executes normally."""
        def fake_invoke_hook(hook_name, **kwargs):
            if hook_name == "pre_tool_call":
                return [
                    "block",
                    {"action": "block"},           # missing message
                    {"action": "deny", "message": "nope"},
                ]
            return []

        monkeypatch.setattr("hermes_cli.plugins.invoke_hook", fake_invoke_hook)
        monkeypatch.setattr("model_tools.registry.dispatch",
                            lambda *a, **kw: json.dumps({"ok": True}))

        result = json.loads(handle_function_call("read_file", {"path": "test.txt"}, task_id="t1"))
        assert result == {"ok": True}


    def test_relay_rewrite_is_visible_to_pre_tool_authorization(self, monkeypatch):
        observed = {}

        def rewrite(**kwargs):
            assert kwargs["tool_name"] == "read_file"
            return {**kwargs["args"], "path": "approved.txt"}

        def fake_invoke_hook(hook_name, **kwargs):
            if hook_name == "pre_tool_call":
                observed["pre_tool_args"] = kwargs["args"]
            return []

        def dispatch(_name, args, **_kwargs):
            observed["dispatch_args"] = args
            return json.dumps({"ok": True})

        monkeypatch.setattr(
            "hermes_cli.observability.relay_runtime.apply_tool_request_intercepts",
            rewrite,
        )
        monkeypatch.setattr("hermes_cli.plugins.invoke_hook", fake_invoke_hook)
        monkeypatch.setattr("hermes_cli.plugins.has_hook", lambda name: True)
        monkeypatch.setattr("model_tools.registry.dispatch", dispatch)

        handle_function_call(
            "read_file",
            {"path": "original.txt"},
            task_id="t1",
            session_id="s1",
        )

        assert observed["pre_tool_args"]["path"] == "approved.txt"
        assert observed["dispatch_args"]["path"] == "approved.txt"



# =========================================================================
# Legacy toolset map
# =========================================================================

class TestLegacyToolsetMap:
    def test_expected_legacy_names(self):
        expected = [
            "web_tools", "terminal_tools", "vision_tools",
            "image_tools", "skills_tools", "browser_tools", "cronjob_tools",
            "file_tools", "tts_tools",
        ]
        for name in expected:
            assert name in _LEGACY_TOOLSET_MAP, f"Missing legacy toolset: {name}"



# =========================================================================
# Backward-compat wrappers
# =========================================================================

class TestBackwardCompat:
    def test_get_all_tool_names_returns_list(self):
        names = get_all_tool_names()
        assert isinstance(names, list)
        assert len(names) > 0
        # Should contain well-known tools
        assert "web_search" in names
        assert "terminal" in names

    def test_get_toolset_for_tool(self):
        result = get_toolset_for_tool("web_search")
        assert result is not None
        assert isinstance(result, str)




# =========================================================================
# _coerce_number — inf / nan must fall through to the original string
# (regression: fix: eliminate duplicate checkpoint entries and JSON-unsafe coercion)
# =========================================================================

class TestCoerceNumberInfNan:
    """_coerce_number must honor its documented contract ("Returns original
    string on failure") for inf/nan inputs, because float('inf') and
    float('nan') are not JSON-compliant under strict serialization."""

    def test_inf_returns_original_string(self):
        from model_tools import _coerce_number
        assert _coerce_number("inf") == "inf"


    def test_nan_returns_original_string(self):
        from model_tools import _coerce_number
        assert _coerce_number("nan") == "nan"



    def test_normal_numbers_still_coerce(self):
        """Guard against over-correction — real numbers still coerce."""
        from model_tools import _coerce_number
        assert _coerce_number("42") == 42
        assert _coerce_number("3.14") == 3.14
        assert _coerce_number("1e3") == 1000

class TestDisabledToolsetsPlatformBundle:
    """Regression test for #33924: disabling a platform bundle (hermes-*)
    must not remove core tools from other enabled toolsets."""

    def test_disabling_platform_bundle_preserves_core_tools(self):
        """Disabling hermes-yuanbao should not strip core tools from hermes-telegram."""
        from model_tools import get_tool_definitions

        tools_telegram = get_tool_definitions(
            enabled_toolsets=["hermes-telegram"],
            quiet_mode=True,
        )
        tools_telegram_no_yuanbao = get_tool_definitions(
            enabled_toolsets=["hermes-telegram"],
            disabled_toolsets=["hermes-yuanbao"],
            quiet_mode=True,
        )
        names_telegram = {t["function"]["name"] for t in tools_telegram}
        names_no_yuanbao = {t["function"]["name"] for t in tools_telegram_no_yuanbao}

        # Disabling a *different* platform bundle must not remove any tools
        assert names_telegram == names_no_yuanbao, (
            f"Tools lost after disabling hermes-yuanbao: "
            f"{names_telegram - names_no_yuanbao}"
        )

    def test_disabling_platform_bundle_removes_own_tools(self):
        """Disabling hermes-discord should remove discord-specific tools."""
        from model_tools import get_tool_definitions

        tools = get_tool_definitions(
            enabled_toolsets=["hermes-discord"],
            disabled_toolsets=["hermes-discord"],
            quiet_mode=True,
        )
        names = {t["function"]["name"] for t in tools}
        assert "discord" not in names




    def test_bundle_non_core_tools_unknown_falls_back(self):
        """An unknown/garbage bundle name falls back to full resolution (best effort)."""
        from toolsets import bundle_non_core_tools
        # A non-existent bundle resolves to an empty set (no tools), not a crash.
        assert bundle_non_core_tools("hermes-does-not-exist") == set()


class TestDisabledToolsetsPostureToolset:
    """Regression test for #57315: disabling a posture toolset (`coding`,
    posture: True) must preserve the shared core tools it re-lists but does
    not own -- same non-core-delta subtraction as hermes-* bundles (#33924) --
    while atomic toolsets stay fully removable."""

    def test_disabling_coding_preserves_core_but_atomic_disables_still_remove(self):
        from model_tools import get_tool_definitions

        # web_search is check_fn-gated (needs an API key); probe only the core
        # tools actually present in baseline so gating cannot mask the fix.
        core_probe = {"terminal", "read_file", "write_file", "web_search", "execute_code"}

        baseline = {
            t["function"]["name"]
            for t in get_tool_definitions(quiet_mode=True)
        }
        present_core = core_probe & baseline
        # Sanity: at least some probed core tools are available in this env.
        assert present_core, "no probed core tools present in baseline"

        no_coding = {
            t["function"]["name"]
            for t in get_tool_definitions(
                disabled_toolsets=["coding"], quiet_mode=True
            )
        }
        # Previously the full resolve_toolset("coding") subtraction stripped
        # these shared core tools, collapsing the schema to a handful (#57315).
        assert present_core <= no_coding, (
            f"Core tools stripped by disabling 'coding': {present_core - no_coding}"
        )

        # Atomic (non-posture) toolsets must still be fully removable.
        no_terminal = {
            t["function"]["name"]
            for t in get_tool_definitions(
                disabled_toolsets=["terminal"], quiet_mode=True
            )
        }
        assert "terminal" not in no_terminal

        no_file = {
            t["function"]["name"]
            for t in get_tool_definitions(
                disabled_toolsets=["file"], quiet_mode=True
            )
        }
        assert "write_file" not in no_file
