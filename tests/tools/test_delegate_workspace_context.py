from __future__ import annotations

import json
import logging
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _parent(task_id: str):
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "test-key"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "test-model"
    parent.platform = "cli"
    parent.enabled_toolsets = ["terminal", "file"]
    parent.disabled_toolsets = []
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent.provider_require_parameters = False
    parent.provider_data_collection = None
    parent.openrouter_min_coding_score = None
    parent.request_overrides = {}
    parent.reasoning_config = None
    parent.prefill_messages = None
    parent._fallback_chain = None
    parent._session_db = None
    parent._memory_manager = None
    parent._delegate_depth = 0
    parent._current_task_id = task_id
    parent._current_turn_id = "parent-turn-id"
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._interrupt_requested = False
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    parent.session_id = "parent-session"
    parent.max_tokens = None
    parent.acp_command = None
    parent.acp_args = []
    parent._client_kwargs = {
        "base_url": parent.base_url,
        "api_key": parent.api_key,
    }
    parent.session_estimated_cost_usd = 0.0
    parent.session_cost_source = "none"
    parent.session_cost_status = "unknown"
    return parent


def _runnable_child(*, session_id: str = "child-session") -> MagicMock:
    child = MagicMock()
    child.session_id = session_id
    child._session_init_model_config = {}
    child._credential_pool = None
    child.model = "test-model"
    child.tool_progress_callback = None
    child.session_prompt_tokens = 0
    child.session_completion_tokens = 0
    child.session_reasoning_tokens = 0
    child.session_estimated_cost_usd = 0.0
    child.get_activity_summary.return_value = {
        "current_tool": None,
        "api_call_count": 0,
        "max_iterations": 1,
    }
    return child


def _completed(summary: str = "done") -> dict:
    return {
        "final_response": summary,
        "completed": True,
        "interrupted": False,
        "api_calls": 1,
        "messages": [],
    }


def _delegation_config() -> dict:
    return {
        "max_concurrent_children": 3,
        "max_spawn_depth": 2,
        "orchestrator_enabled": True,
    }


def _inherited_credentials() -> dict:
    return {
        "provider": None,
        "base_url": None,
        "api_key": None,
        "api_mode": None,
        "model": None,
        "request_overrides": None,
        "max_output_tokens": None,
    }


def test_child_provider_prompt_uses_parent_task_workspace(monkeypatch, tmp_path):
    from tools import terminal_tool
    from tools.delegate_tool import _build_child_agent

    parent_workspace = tmp_path / "parent-workspace"
    global_workspace = tmp_path / "global-workspace"
    parent_workspace.mkdir()
    global_workspace.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(global_workspace))
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    terminal_tool.record_session_cwd("parent-turn", str(parent_workspace))

    with patch("run_agent.AIAgent") as mock_agent:
        mock_agent.return_value = MagicMock()
        _build_child_agent(
            task_index=0,
            goal="Inspect the delegated workspace",
            context="Keep the parent-provided constraints.",
            toolsets=None,
            model=None,
            max_iterations=5,
            task_count=1,
            parent_agent=_parent("parent-turn"),
        )

    prompt = mock_agent.call_args.kwargs["ephemeral_system_prompt"]
    assert "YOUR TASK:\nInspect the delegated workspace" in prompt
    assert "CONTEXT:\nKeep the parent-provided constraints." in prompt
    assert f"WORKSPACE PATH:\n{parent_workspace.resolve()}" in prompt
    assert str(global_workspace) not in prompt


def test_child_runtime_cwd_is_seeded_from_parent_runtime_context(
    monkeypatch, tmp_path
):
    from agent import runtime_cwd
    from tools import file_tools, terminal_tool
    from tools.delegate_tool import _build_child_agent, _run_single_child

    parent_workspace = tmp_path / "parent-workspace"
    global_workspace = tmp_path / "global-workspace"
    parent_workspace.mkdir()
    global_workspace.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(global_workspace))
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(file_tools, "_file_ops_cache", {})

    observed = {}
    child = _runnable_child()

    def run_conversation(**kwargs):
        task_id = kwargs["task_id"]
        observed["record"] = terminal_tool.get_session_cwd(task_id)
        observed["file"] = str(file_tools._resolve_base_dir(task_id))
        observed["terminal"] = terminal_tool._resolve_command_cwd(
            workdir=None,
            default_cwd=str(global_workspace),
            session_key=task_id,
        )
        return {
            "final_response": "done",
            "completed": True,
            "interrupted": False,
            "api_calls": 1,
            "messages": [],
        }

    child.run_conversation.side_effect = run_conversation
    parent = _parent("parent-turn")
    token = runtime_cwd.set_session_cwd(str(parent_workspace))
    try:
        with patch("run_agent.AIAgent", return_value=child):
            built_child = _build_child_agent(
                task_index=0,
                goal="Use the parent workspace",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=5,
                task_count=1,
                parent_agent=parent,
            )
        result = _run_single_child(
            task_index=0,
            goal="Use the parent workspace",
            child=built_child,
            parent_agent=parent,
        )
    finally:
        runtime_cwd._SESSION_CWD.reset(token)

    assert result["status"] == "completed"
    assert observed == {
        "record": str(parent_workspace.resolve()),
        "file": str(parent_workspace.resolve()),
        "terminal": str(parent_workspace.resolve()),
    }


def test_single_child_uses_one_request_snapshot_when_parent_workspace_changes(
    monkeypatch, tmp_path
):
    from agent import runtime_cwd
    from tools import file_tools, terminal_tool
    from tools.delegate_tool import _build_child_agent, _run_single_child

    advertised_workspace = tmp_path / "advertised-workspace"
    later_workspace = tmp_path / "later-workspace"
    global_workspace = tmp_path / "global-workspace"
    for directory in (advertised_workspace, later_workspace, global_workspace):
        directory.mkdir()

    monkeypatch.setenv("TERMINAL_CWD", str(global_workspace))
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(file_tools, "_file_ops_cache", {})
    terminal_tool.record_session_cwd("parent-turn", str(advertised_workspace))

    observed = {}
    child = _runnable_child()

    def run_conversation(**kwargs):
        task_id = kwargs["task_id"]
        observed["user_message"] = kwargs["user_message"]
        observed["record"] = terminal_tool.get_session_cwd(task_id)
        observed["runtime"] = str(runtime_cwd.resolve_agent_cwd())
        observed["file"] = str(file_tools._resolve_base_dir(task_id))
        observed["terminal"] = terminal_tool._resolve_command_cwd(
            workdir=None,
            default_cwd=str(global_workspace),
            session_key=task_id,
        )
        return _completed()

    child.run_conversation.side_effect = run_conversation
    parent = _parent("parent-turn")
    with patch("run_agent.AIAgent", return_value=child) as mock_agent:
        built_child = _build_child_agent(
            task_index=0,
            goal="Original request-local goal",
            context="Original request-local context",
            toolsets=None,
            model=None,
            max_iterations=5,
            task_count=1,
            parent_agent=parent,
        )

    prompt = mock_agent.call_args.kwargs["ephemeral_system_prompt"]
    terminal_tool.record_session_cwd("parent-turn", str(later_workspace))

    result = _run_single_child(
        task_index=0,
        goal="mutated goal must not replace the child snapshot",
        child=built_child,
        parent_agent=parent,
    )

    expected = str(advertised_workspace.resolve())
    assert result["status"] == "completed"
    assert f"WORKSPACE PATH:\n{expected}" in prompt
    assert "YOUR TASK:\nOriginal request-local goal" in prompt
    assert "CONTEXT:\nOriginal request-local context" in prompt
    assert str(later_workspace) not in prompt
    assert observed == {
        "user_message": "Original request-local goal",
        "record": expected,
        "runtime": expected,
        "file": expected,
        "terminal": expected,
    }


def test_child_fails_before_provider_call_when_advertised_workspace_cannot_seed(
    monkeypatch, tmp_path
):
    from tools import terminal_tool
    from tools.delegate_tool import _build_child_agent, _run_single_child

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    terminal_tool.record_session_cwd("parent-turn", str(workspace))

    child = _runnable_child()
    child.run_conversation.return_value = _completed()
    parent = _parent("parent-turn")
    with patch("run_agent.AIAgent", return_value=child):
        built_child = _build_child_agent(
            task_index=0,
            goal="Do not run on an unseeded fallback",
            context="Workspace identity is mandatory.",
            toolsets=None,
            model=None,
            max_iterations=5,
            task_count=1,
            parent_agent=parent,
        )

    def fail_record(_task_id, _cwd):
        raise OSError("record store unavailable")

    monkeypatch.setattr(terminal_tool, "record_session_cwd", fail_record)
    result = _run_single_child(0, "changed", built_child, parent)

    assert result["status"] == "error"
    assert "workspace" in result["error"].lower()
    child.run_conversation.assert_not_called()


def test_workspace_resolver_logs_expected_session_context_failure(
    monkeypatch, tmp_path, caplog
):
    from gateway import session_context
    from tools.delegate_tool import _resolve_workspace_hint

    workspace = tmp_path / "fallback-workspace"
    workspace.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(workspace))

    parent = _parent("")
    parent._current_task_id = None
    parent.session_id = None

    def fail_session_env(_name, _default):
        raise RuntimeError("request context unavailable")

    monkeypatch.setattr(session_context, "get_session_env", fail_session_env)
    with caplog.at_level(logging.DEBUG, logger="tools.delegate_tool"):
        resolved = _resolve_workspace_hint(parent)

    assert resolved == str(workspace.resolve())
    assert "Could not read request-local workspace session key" in caplog.text


def test_workspace_resolver_checks_all_exact_sessions_before_default_fallback(
    monkeypatch, tmp_path
):
    from gateway import session_context
    from tools import terminal_tool
    from tools.delegate_tool import _resolve_workspace_hint

    default_workspace = tmp_path / "default-workspace"
    later_session_workspace = tmp_path / "later-session-workspace"
    default_workspace.mkdir()
    later_session_workspace.mkdir()

    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    monkeypatch.setattr(
        terminal_tool,
        "_task_env_overrides",
        {
            "default": {"cwd": str(default_workspace)},
            "later-session": {"cwd": str(later_session_workspace)},
        },
    )
    monkeypatch.setattr(session_context, "get_session_env", lambda *_args: "")
    parent = SimpleNamespace(
        _current_task_id="earlier-task",
        session_id="later-session",
    )

    resolved = _resolve_workspace_hint(parent)

    assert resolved == str(later_session_workspace.resolve())


def test_workspace_resolver_preserves_valid_non_host_container_path(monkeypatch):
    from gateway import session_context
    from tools import terminal_tool
    from tools.delegate_tool import _resolve_workspace_hint

    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(session_context, "get_session_env", lambda *_args: "")
    terminal_tool.record_session_cwd("container-task", "/workspace/task42")
    parent = SimpleNamespace(
        _current_task_id="container-task",
        session_id=None,
    )

    resolved = _resolve_workspace_hint(parent)

    assert resolved == "/workspace/task42"


def test_workspace_resolver_rejects_stale_authoritative_session_record(
    monkeypatch, tmp_path
):
    from gateway import session_context
    from tools import terminal_tool
    from tools.delegate_tool import _resolve_workspace_hint

    fallback_workspace = tmp_path / "fallback-workspace"
    fallback_workspace.mkdir()
    missing_workspace = tmp_path / "missing-workspace"
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(fallback_workspace))
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(session_context, "get_session_env", lambda *_args: "")
    terminal_tool.record_session_cwd("parent-turn", str(missing_workspace))
    parent = SimpleNamespace(
        _current_task_id="parent-turn",
        session_id=None,
    )

    with pytest.raises(RuntimeError, match="session cwd record.*invalid"):
        _resolve_workspace_hint(parent)


def test_workspace_resolver_rejects_stale_request_local_runtime_cwd(
    monkeypatch, tmp_path
):
    from agent import runtime_cwd
    from gateway import session_context
    from tools import terminal_tool
    from tools.delegate_tool import _resolve_workspace_hint

    fallback_workspace = tmp_path / "fallback-workspace"
    fallback_workspace.mkdir()
    missing_workspace = tmp_path / "missing-runtime-workspace"
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(fallback_workspace))
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(session_context, "get_session_env", lambda *_args: "")
    parent = SimpleNamespace(_current_task_id=None, session_id=None)

    token = runtime_cwd.set_session_cwd(str(missing_workspace))
    try:
        with pytest.raises(RuntimeError, match="request-local runtime cwd.*invalid"):
            _resolve_workspace_hint(parent)
    finally:
        runtime_cwd._SESSION_CWD.reset(token)


@pytest.mark.parametrize("attribute", ["terminal_cwd", "cwd"])
@pytest.mark.parametrize("candidate", ["", "."])
def test_workspace_resolver_rejects_explicit_invalid_parent_workspace(
    monkeypatch, tmp_path, attribute, candidate
):
    from gateway import session_context
    from tools import terminal_tool
    from tools.delegate_tool import _resolve_workspace_hint

    fallback_workspace = tmp_path / "fallback-workspace"
    fallback_workspace.mkdir()
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(fallback_workspace))
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(session_context, "get_session_env", lambda *_args: "")
    parent = SimpleNamespace(_current_task_id=None, session_id=None)
    setattr(parent, attribute, candidate)

    with pytest.raises(RuntimeError, match=f"parent {attribute}.*invalid"):
        _resolve_workspace_hint(parent)


@pytest.mark.parametrize("candidate", ["", "."])
def test_workspace_resolver_rejects_explicit_invalid_terminal_cwd(
    monkeypatch, candidate
):
    from agent import runtime_cwd
    from gateway import session_context
    from tools import terminal_tool
    from tools.delegate_tool import _resolve_workspace_hint

    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", candidate)
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(session_context, "get_session_env", lambda *_args: "")
    monkeypatch.setattr(runtime_cwd, "resolve_context_cwd", lambda: None)
    parent = SimpleNamespace(_current_task_id=None, session_id=None)

    with pytest.raises(RuntimeError, match="TERMINAL_CWD.*invalid"):
        _resolve_workspace_hint(parent)


def test_run_single_child_fails_without_immutable_request_snapshot():
    from tools.delegate_tool import _run_single_child

    child = _runnable_child()
    child.run_conversation.return_value = _completed()

    result = _run_single_child(
        task_index=0,
        goal="must not manufacture a request snapshot",
        child=child,
        parent_agent=_parent("parent-turn"),
    )

    assert result["status"] == "error"
    assert "immutable delegation request snapshot" in result["error"]
    child.run_conversation.assert_not_called()


def test_batch_children_keep_distinct_goal_context_and_workspace_snapshots(
    monkeypatch, tmp_path
):
    from tools import delegate_tool, file_tools, terminal_tool

    original_workspace = tmp_path / "batch-original"
    second_workspace = tmp_path / "batch-second"
    execution_workspace = tmp_path / "batch-execution"
    global_workspace = tmp_path / "batch-global"
    for directory in (
        original_workspace,
        second_workspace,
        execution_workspace,
        global_workspace,
    ):
        directory.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(global_workspace))
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(file_tools, "_file_ops_cache", {})
    terminal_tool.record_session_cwd("batch-parent-turn", str(original_workspace))

    monkeypatch.setattr(delegate_tool, "_load_config", _delegation_config)
    monkeypatch.setattr(
        delegate_tool,
        "_resolve_delegation_credentials",
        lambda *_args, **_kwargs: _inherited_credentials(),
    )

    captures = []
    capture_lock = threading.Lock()
    built_count = 0

    def child_factory(*_args, **kwargs):
        nonlocal built_count
        prompt = kwargs["ephemeral_system_prompt"]
        child = _runnable_child(session_id=f"batch-child-{built_count}")
        built_count += 1

        def run_conversation(**run_kwargs):
            task_id = run_kwargs["task_id"]
            with capture_lock:
                captures.append(
                    {
                        "prompt": prompt,
                        "goal": run_kwargs["user_message"],
                        "record": terminal_tool.get_session_cwd(task_id),
                        "file": str(file_tools._resolve_base_dir(task_id)),
                        "terminal": terminal_tool._resolve_command_cwd(
                            workdir=None,
                            default_cwd=str(global_workspace),
                            session_key=task_id,
                        ),
                    }
                )
            return _completed(run_kwargs["user_message"])

        child.run_conversation.side_effect = run_conversation
        return child

    original_build = delegate_tool._build_child_preserving_parent_tools

    def build_then_move_parent(**kwargs):
        child = original_build(**kwargs)
        if kwargs["task_index"] == 0:
            terminal_tool.record_session_cwd(
                "batch-parent-turn", str(second_workspace)
            )
        if kwargs["task_index"] == 1:
            terminal_tool.record_session_cwd(
                "batch-parent-turn", str(execution_workspace)
            )
        return child

    parent = _parent("batch-parent-turn")
    with patch("run_agent.AIAgent", side_effect=child_factory):
        monkeypatch.setattr(
            delegate_tool,
            "_build_child_preserving_parent_tools",
            build_then_move_parent,
        )
        payload = json.loads(
            delegate_tool.delegate_task(
                tasks=[
                    {"goal": "alpha-goal", "context": "alpha-context-only"},
                    {"goal": "beta-goal", "context": "beta-context-only"},
                ],
                parent_agent=parent,
            )
        )

    assert [entry["status"] for entry in payload["results"]] == [
        "completed",
        "completed",
    ]
    assert len(captures) == 2
    by_goal = {capture["goal"]: capture for capture in captures}
    assert set(by_goal) == {"alpha-goal", "beta-goal"}
    for (
        goal,
        own_context,
        sibling_context,
        expected_workspace,
        sibling_workspace,
    ) in (
        (
            "alpha-goal",
            "alpha-context-only",
            "beta-context-only",
            original_workspace,
            second_workspace,
        ),
        (
            "beta-goal",
            "beta-context-only",
            "alpha-context-only",
            second_workspace,
            original_workspace,
        ),
    ):
        capture = by_goal[goal]
        expected = str(expected_workspace.resolve())
        assert f"YOUR TASK:\n{goal}" in capture["prompt"]
        assert f"CONTEXT:\n{own_context}" in capture["prompt"]
        assert sibling_context not in capture["prompt"]
        assert f"WORKSPACE PATH:\n{expected}" in capture["prompt"]
        assert str(sibling_workspace.resolve()) not in capture["prompt"]
        assert str(execution_workspace.resolve()) not in capture["prompt"]
        assert capture["record"] == expected
        assert capture["file"] == expected
        assert capture["terminal"] == expected


def test_nested_child_inherits_orchestrator_request_workspace_snapshot(
    monkeypatch, tmp_path
):
    from tools import delegate_tool, terminal_tool

    original_workspace = tmp_path / "nested-original"
    later_workspace = tmp_path / "nested-later"
    original_workspace.mkdir()
    later_workspace.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(later_workspace))
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    terminal_tool.record_session_cwd("root-turn", str(original_workspace))

    monkeypatch.setattr(delegate_tool, "_load_config", _delegation_config)
    monkeypatch.setattr(
        delegate_tool,
        "_resolve_delegation_credentials",
        lambda *_args, **_kwargs: _inherited_credentials(),
    )

    built_prompts = []
    leaf_observed = {}

    def child_factory(*_args, **kwargs):
        prompt = kwargs["ephemeral_system_prompt"]
        built_prompts.append(prompt)
        child = _runnable_child(session_id=f"nested-child-{len(built_prompts)}")
        child.base_url = kwargs["base_url"]
        child.api_key = kwargs["api_key"]
        child.provider = kwargs["provider"]
        child.api_mode = kwargs["api_mode"]
        child.enabled_toolsets = list(kwargs["enabled_toolsets"] or [])
        child.disabled_toolsets = list(kwargs["disabled_toolsets"] or [])
        child._active_children = []
        child._active_children_lock = threading.Lock()
        child._memory_manager = None
        child._session_db = None
        child._print_fn = None
        child.thinking_callback = None
        child.max_tokens = None
        child.acp_command = None
        child.acp_args = []
        child._client_kwargs = {}
        child.session_cost_source = "none"
        child.session_cost_status = "unknown"

        if "Orchestrator Role" in prompt:

            def run_orchestrator(**run_kwargs):
                child._current_task_id = run_kwargs["task_id"]
                nested = json.loads(
                    delegate_tool.delegate_task(
                        goal="leaf-goal",
                        context="leaf-context-only",
                        parent_agent=child,
                    )
                )
                assert nested["results"][0]["status"] == "completed"
                return _completed("orchestrated")

            child.run_conversation.side_effect = run_orchestrator
        else:

            def run_leaf(**run_kwargs):
                task_id = run_kwargs["task_id"]
                leaf_observed.update(
                    {
                        "goal": run_kwargs["user_message"],
                        "record": terminal_tool.get_session_cwd(task_id),
                    }
                )
                return _completed("leaf done")

            child.run_conversation.side_effect = run_leaf
        return child

    root_parent = _parent("root-turn")
    root_parent.enabled_toolsets = ["terminal", "file", "delegation"]
    original_build = delegate_tool._build_child_preserving_parent_tools

    def build_then_move_root(**kwargs):
        child = original_build(**kwargs)
        if kwargs["parent_agent"] is root_parent:
            terminal_tool.record_session_cwd("root-turn", str(later_workspace))
        return child

    with patch("run_agent.AIAgent", side_effect=child_factory):
        monkeypatch.setattr(
            delegate_tool,
            "_build_child_preserving_parent_tools",
            build_then_move_root,
        )
        payload = json.loads(
            delegate_tool.delegate_task(
                goal="orchestrator-goal",
                context="orchestrator-context-only",
                role="orchestrator",
                parent_agent=root_parent,
            )
        )

    expected = str(original_workspace.resolve())
    assert payload["results"][0]["status"] == "completed"
    assert len(built_prompts) == 2
    orchestrator_prompt, leaf_prompt = built_prompts
    assert f"WORKSPACE PATH:\n{expected}" in orchestrator_prompt
    assert f"WORKSPACE PATH:\n{expected}" in leaf_prompt
    assert "orchestrator-context-only" in orchestrator_prompt
    assert "leaf-context-only" not in orchestrator_prompt
    assert "leaf-context-only" in leaf_prompt
    assert "orchestrator-context-only" not in leaf_prompt
    assert leaf_observed == {"goal": "leaf-goal", "record": expected}
