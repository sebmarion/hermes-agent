"""Tests for the subagent_stop hook event.

Covers wire-up from tools.delegate_tool.delegate_task:
  * fires once per child in both single-task and batch modes
  * runs on the parent thread (no re-entrancy for hook authors)
  * carries child_role when the agent exposes _delegate_role
  * carries child_role=None when _delegate_role is not set (pre-M3)
  * exposes a detached, metadata-only tool_call_history
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import tools.delegate_tool as delegate_tool
from tools.delegate_tool import _summarize_tool_arguments, delegate_task
from hermes_cli import plugins


def _make_parent(depth: int = 0, session_id: str = "parent-1"):
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "***"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = depth
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    parent._memory_manager = None
    parent.session_id = session_id
    return parent


@pytest.fixture(autouse=True)
def _fresh_plugin_manager():
    """Each test gets a fresh PluginManager so hook callbacks don't
    leak between tests."""
    original = plugins._plugin_manager
    plugins._plugin_manager = plugins.PluginManager()
    yield
    plugins._plugin_manager = original


@pytest.fixture(autouse=True)
def _stub_child_builder(monkeypatch):
    """Replace _build_child_agent with a MagicMock factory so delegate_task
    never transitively imports run_agent / openai.  Keeps the test runnable
    in environments without heavyweight runtime deps installed."""
    def _fake_build_child(task_index, **kwargs):
        child = MagicMock()
        child._delegate_saved_tool_names = []
        child._credential_pool = None
        return child

    monkeypatch.setattr(
        "tools.delegate_tool._build_child_agent", _fake_build_child,
    )


def _register_capturing_hook():
    captured = []

    def _cb(**kwargs):
        kwargs["_thread"] = threading.current_thread()
        captured.append(kwargs)

    mgr = plugins.get_plugin_manager()
    mgr._hooks.setdefault("subagent_stop", []).append(_cb)
    return captured


# ── single-task mode ──────────────────────────────────────────────────────


class TestSingleTask:
    def test_fires_once(self):
        captured = _register_capturing_hook()

        with patch("tools.delegate_tool._run_single_child") as mock_run:
            mock_run.return_value = {
                "task_index": 0,
                "status": "completed",
                "summary": "Done!",
                "api_calls": 3,
                "duration_seconds": 5.0,
                "_child_role": "analyst",
            }
            delegate_task(goal="do X", parent_agent=_make_parent())

        assert len(captured) == 1
        payload = captured[0]
        assert payload["child_role"] == "analyst"
        assert payload["child_status"] == "completed"
        assert payload["child_summary"] == "Done!"
        assert payload["duration_ms"] == 5000

    def test_fires_on_parent_thread(self):
        captured = _register_capturing_hook()
        main_thread = threading.current_thread()

        with patch("tools.delegate_tool._run_single_child") as mock_run:
            mock_run.return_value = {
                "task_index": 0, "status": "completed",
                "summary": "x", "api_calls": 1, "duration_seconds": 0.1,
                "_child_role": None,
            }
            delegate_task(goal="go", parent_agent=_make_parent())

        assert captured[0]["_thread"] is main_thread

    def test_payload_includes_parent_session_id(self):
        captured = _register_capturing_hook()

        with patch("tools.delegate_tool._run_single_child") as mock_run:
            mock_run.return_value = {
                "task_index": 0, "status": "completed",
                "summary": "x", "api_calls": 1, "duration_seconds": 0.1,
                "_child_role": None,
            }
            delegate_task(
                goal="go",
                parent_agent=_make_parent(session_id="sess-xyz"),
            )

        assert captured[0]["parent_session_id"] == "sess-xyz"

    def test_reaches_first_party_observer_and_plugin_once(self):
        captured = _register_capturing_hook()

        with (
            patch("tools.delegate_tool._run_single_child") as mock_run,
            patch("hermes_cli.observability.observe_lifecycle") as observe,
        ):
            mock_run.return_value = {
                "task_index": 0,
                "status": "completed",
                "summary": "Done!",
                "api_calls": 1,
                "duration_seconds": 0.1,
                "_child_role": "leaf",
            }
            delegate_task(goal="do X", parent_agent=_make_parent())

        assert len(captured) == 1
        observe.assert_called_once()
        assert observe.call_args.args == ("subagent_stop",)
        assert observe.call_args.kwargs["child_goal"] == "do X"

    def test_shared_emitter_is_exact_once_under_concurrency(self):
        captured = _register_capturing_hook()
        child = SimpleNamespace(
            session_id="child-1",
            _delegate_role="leaf",
            _delegate_lane="code_worker",
            _delegate_provider="local",
            _delegate_model="worker-model",
            _delegate_mode="execute",
        )
        result = {
            "task_index": 0,
            "status": "completed",
            "summary": "done",
            "duration_seconds": 0.1,
        }
        emitter = getattr(delegate_tool, "_emit_subagent_stop_once")

        with patch("hermes_cli.observability.observe_lifecycle") as observe:
            with ThreadPoolExecutor(max_workers=8) as executor:
                emitted = list(
                    executor.map(
                        lambda _: emitter(
                            _make_parent(),
                            child,
                            child_goal="race-safe",
                            result=dict(result),
                        ),
                        range(8),
                    )
                )

        assert emitted.count(True) == 1
        assert emitted.count(False) == 7
        assert len(captured) == 1
        observe.assert_called_once()


# ── batch mode ────────────────────────────────────────────────────────────


class TestBatchMode:
    def test_fires_per_child(self):
        captured = _register_capturing_hook()

        with patch("tools.delegate_tool._run_single_child") as mock_run:
            mock_run.side_effect = [
                {"task_index": 0, "status": "completed",
                 "summary": "A", "api_calls": 1, "duration_seconds": 1.0,
                 "_child_role": "role-a"},
                {"task_index": 1, "status": "completed",
                 "summary": "B", "api_calls": 2, "duration_seconds": 2.0,
                 "_child_role": "role-b"},
                {"task_index": 2, "status": "completed",
                 "summary": "C", "api_calls": 3, "duration_seconds": 3.0,
                 "_child_role": "role-c"},
            ]
            delegate_task(
                tasks=[
                    {"goal": "A"}, {"goal": "B"}, {"goal": "C"},
                ],
                parent_agent=_make_parent(),
            )

        assert len(captured) == 3
        roles = sorted(c["child_role"] for c in captured)
        assert roles == ["role-a", "role-b", "role-c"]

    def test_all_fires_on_parent_thread(self):
        captured = _register_capturing_hook()
        main_thread = threading.current_thread()

        with patch("tools.delegate_tool._run_single_child") as mock_run:
            mock_run.side_effect = [
                {"task_index": 0, "status": "completed",
                 "summary": "A", "api_calls": 1, "duration_seconds": 1.0,
                 "_child_role": None},
                {"task_index": 1, "status": "completed",
                 "summary": "B", "api_calls": 2, "duration_seconds": 2.0,
                 "_child_role": None},
            ]
            delegate_task(
                tasks=[{"goal": "A"}, {"goal": "B"}],
                parent_agent=_make_parent(),
            )

        for payload in captured:
            assert payload["_thread"] is main_thread

    @pytest.mark.parametrize(
        (
            "goals",
            "parent_depth",
            "combined",
            "terminal_status",
            "expected_status",
            "expected_failure_kind",
            "expected_exit_reason",
        ),
        [
            (
                ["one scheduled child"],
                0,
                {"results": [], "error": "interrupted before execution"},
                "interrupted",
                "interrupted",
                "interrupted",
                "interrupted_before_execution",
            ),
            (
                ["nested child A", "nested child B"],
                1,
                {"results": [], "error": "RuntimeError: runner crashed"},
                "error",
                "failed",
                "async_runner_crash",
                "error",
            ),
            (
                ["stalled child A", "stalled child B"],
                0,
                {
                    "status": "stalled",
                    "summary": None,
                    "error": "detached subagent stopped making progress",
                    "duration_seconds": 10,
                    "exit_reason": "stalled",
                },
                "stalled",
                "stalled",
                "stalled",
                "stalled",
            ),
        ],
    )
    def test_background_terminal_exits_emit_once_per_built_child(
        self,
        monkeypatch,
        goals,
        parent_depth,
        combined,
        terminal_status,
        expected_status,
        expected_failure_kind,
        expected_exit_reason,
    ):
        captured = _register_capturing_hook()
        built_children = []
        dispatch_kwargs = {}

        def build_child(task_index, **_kwargs):
            child = SimpleNamespace(
                session_id=f"child-{task_index}",
                _delegate_role="leaf",
                provider="local-provider",
                model="local-model",
            )
            built_children.append(child)
            return child

        def dispatch_batch(**kwargs):
            dispatch_kwargs.update(kwargs)
            return {"status": "dispatched", "delegation_id": "deleg-test"}

        monkeypatch.setattr(delegate_tool, "_build_child_agent", build_child)
        monkeypatch.setattr(delegate_tool, "_get_max_spawn_depth", lambda: 2)
        monkeypatch.setattr(
            "gateway.session_context.async_delivery_supported", lambda: True
        )
        monkeypatch.setattr(
            "tools.async_delegation.dispatch_async_delegation_batch", dispatch_batch
        )
        parent = _make_parent(depth=parent_depth, session_id="nested-parent")

        with patch("hermes_cli.observability.observe_lifecycle") as observe:
            if len(goals) == 1:
                raw = delegate_task(
                    goal=goals[0], background=True, parent_agent=parent
                )
            else:
                raw = delegate_task(
                    tasks=[{"goal": goal} for goal in goals],
                    background=True,
                    parent_agent=parent,
                )
            assert json.loads(raw)["status"] == "dispatched"
            terminal_callback = dispatch_kwargs["terminal_callback"]
            terminal_callback(dict(combined), terminal_status)
            terminal_callback(dict(combined), terminal_status)

        assert len(built_children) == len(goals)
        assert len(captured) == len(goals)
        assert observe.call_count == len(goals)
        assert [payload["child_goal"] for payload in captured] == goals
        for child, payload in zip(built_children, captured):
            assert payload["parent_session_id"] == "nested-parent"
            assert payload["child_status"] == expected_status
            assert payload["child_lane"] == (child._delegate_lane or "")
            assert payload["child_provider"] == child._delegate_provider
            assert payload["child_model"] == child._delegate_model
            assert payload["child_mode"] == "execute"
            assert payload["child_failure_kind"] == expected_failure_kind
            assert payload["child_exit_reason"] == expected_exit_reason


# ── payload shape ─────────────────────────────────────────────────────────


class TestPayloadShape:
    def test_includes_host_owned_route_goal_and_outcome_evidence(self):
        captured = _register_capturing_hook()

        with patch("tools.delegate_tool._run_single_child") as mock_run:
            mock_run.return_value = {
                "task_index": 0,
                "status": "failed",
                "summary": "provider stopped",
                "api_calls": 2,
                "duration_seconds": 0.25,
                "mode": "review",
                "lane": "review_lane",
                "provider": "review-provider",
                "routed_model": "review-model",
                "failure_kind": "provider_error",
                "exit_reason": "error",
                "evidence": {
                    "tool_turn_count": 2,
                    "successful_tool_count": 1,
                },
                "_child_role": "reviewer",
            }
            delegate_task(goal="Review the exact diff", parent_agent=_make_parent())

        payload = captured[0]
        assert payload["child_goal"] == "Review the exact diff"
        assert payload["child_lane"] == "review_lane"
        assert payload["child_provider"] == "review-provider"
        assert payload["child_model"] == "review-model"
        assert payload["child_mode"] == "review"
        assert payload["child_failure_kind"] == "provider_error"
        assert payload["child_exit_reason"] == "error"
        assert payload["child_successful_tool_count"] == 1

    def test_includes_redacted_tool_call_history(self):
        captured = _register_capturing_hook()

        with patch("tools.delegate_tool._run_single_child") as mock_run:
            mock_run.return_value = {
                "task_index": 0,
                "status": "completed",
                "summary": "wrote the report",
                "api_calls": 1,
                "duration_seconds": 0.1,
                "tool_trace": [{
                    "tool": "write_file",
                    "args_bytes": 128,
                    "result_bytes": 32,
                    "status": "ok",
                    "input_summary": {
                        "argument_keys": ["content", "path", "token"],
                        "targets": {
                            "path": "/private/report.json",
                            "url": "https://user:password@example.test:8443/upload?token=secret",
                            "token": "must-not-leak",
                        },
                    },
                    "args": {"path": "/private/report.json"},
                    "result": "secret output",
                }],
            }
            delegate_task(goal="do X", parent_agent=_make_parent())

        assert captured[0]["tool_call_history"] == [{
            "tool_name": "write_file",
            "tool_input": {
                "argument_keys": ["content", "path", "token"],
                "targets": {
                    "path": "/private/report.json",
                    "url": "https://example.test:8443/upload",
                },
            },
            "input_bytes": 128,
            "output_bytes": 32,
            "status": "ok",
        }]




    def test_result_does_not_leak_child_role_field(self):
        """The internal _child_role key must be stripped before the
        result dict is serialised to JSON."""
        _register_capturing_hook()

        with patch("tools.delegate_tool._run_single_child") as mock_run:
            mock_run.return_value = {
                "task_index": 0, "status": "completed",
                "summary": "x", "api_calls": 1, "duration_seconds": 0.1,
                "_child_role": "leaf",
            }
            raw = delegate_task(goal="do X", parent_agent=_make_parent())

        parsed = json.loads(raw)
        assert "results" in parsed
        assert "_child_role" not in parsed["results"][0]
