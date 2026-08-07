"""Execution-first contracts for deterministic delegate_task routing."""

import json
from unittest.mock import MagicMock

import pytest

from tools import delegate_tool


def _config():
    return {
        "lanes": {
            "code_worker": {
                "provider": "custom:code",
                "model": "code-model",
                "toolsets": ["terminal", "file"],
            },
            "smart_reviewer": {
                "provider": "review-provider",
                "model": "review-model",
                "toolsets": ["read_only_files"],
            },
            "local_worker": {
                "provider": "custom:local",
                "model": "reason-model",
                "toolsets": ["read_only_files"],
            },
        },
        "tier_routes": {"large": "smart_reviewer"},
        # Must not influence the new execution-first routing contract.
        "default_lane": "local_worker",
    }


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        ({}, "code_worker"),
        ({"mode": "execute"}, "code_worker"),
        ({"mode": "review"}, "smart_reviewer"),
        ({"mode": "reason"}, "local_worker"),
        ({"mode": "execute", "model_tier": "large"}, "smart_reviewer"),
        (
            {
                "mode": "execute",
                "model_tier": "large",
                "route": "local_worker",
            },
            "local_worker",
        ),
    ],
)
def test_route_precedence_is_explicit_then_tier_then_mode(task, expected):
    assert delegate_tool._resolve_lane_for_task(task, _config()) == expected


def test_batch_mode_is_exposed_as_the_exact_model_facing_enum():
    task_properties = delegate_tool.DELEGATE_TASK_SCHEMA["parameters"]["properties"][
        "tasks"
    ]["items"]["properties"]

    assert task_properties["mode"] == {
        "type": "string",
        "enum": ["execute", "review", "reason"],
        "description": (
            "Task intent used by delegation.mode_routes when route and "
            "model_tier are omitted."
        ),
    }
    assert delegate_tool._resolve_lane_for_task(
        {"mode": "review"}, _config()
    ) == "smart_reviewer"


def test_unknown_mode_and_unmapped_tier_fail_closed():
    with pytest.raises(ValueError, match="mode 'write'"):
        delegate_tool._resolve_lane_for_task({"mode": "write"}, _config())
    with pytest.raises(ValueError, match="model_tier 'micro'.*not mapped"):
        delegate_tool._resolve_lane_for_task({"model_tier": "micro"}, _config())


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (None, None),
        (0, None),
        (-1, None),
        (1, 30.0),
        (45, 45.0),
    ],
)
def test_child_timeout_disabled_and_positive_floor_semantics(
    monkeypatch, configured, expected
):
    cfg = {} if configured is None else {"child_timeout_seconds": configured}
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: cfg)
    monkeypatch.delenv("DELEGATION_CHILD_TIMEOUT_SECONDS", raising=False)

    assert delegate_tool._get_child_timeout() == expected


def test_explicit_zero_timeout_overrides_stale_environment_cap(monkeypatch):
    monkeypatch.setattr(
        delegate_tool,
        "_load_config",
        lambda: {"child_timeout_seconds": 0},
    )
    monkeypatch.setenv("DELEGATION_CHILD_TIMEOUT_SECONDS", "600")

    assert delegate_tool._get_child_timeout() is None


def _parent():
    parent = MagicMock()
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = None
    parent.enabled_toolsets = ["terminal", "file", "read_only_files", "delegation"]
    parent.valid_tool_names = {"terminal", "read_file", "write_file", "delegate_task"}
    parent.tool_progress_callback = None
    parent._session_db = None
    parent._memory_manager = None
    parent.session_estimated_cost_usd = 0.0
    return parent


def _successful_entry(index=0):
    return {
        "task_index": index,
        "status": "completed",
        "summary": "done",
        "api_calls": 1,
        "duration_seconds": 0.01,
        "_child_role": "leaf",
    }


def test_delegate_assigns_execution_mode_and_routing_metadata(monkeypatch):
    cfg = _config()
    built = []

    monkeypatch.setattr(delegate_tool, "_load_config", lambda: cfg)
    monkeypatch.setattr(
        delegate_tool,
        "_resolve_delegation_credentials_for_task",
        lambda config, parent, task, *, resolved_lane: {
            "lane": resolved_lane,
            "provider": "provider",
            "model": "model",
            "base_url": "https://example.invalid/v1",
            "api_key": "key",
            "api_mode": "chat_completions",
            "toolsets": config["lanes"][resolved_lane]["toolsets"],
        },
    )

    def fake_build(**kwargs):
        child = MagicMock()
        child._delegate_role = "leaf"
        built.append(child)
        return child

    monkeypatch.setattr(delegate_tool, "_build_child_agent", fake_build)
    monkeypatch.setattr(
        delegate_tool,
        "_run_single_child",
        lambda index, goal, child, parent: {
            **_successful_entry(index),
            "mode": child._delegate_mode,
            "lane": child._delegate_lane,
            "provider": child._delegate_provider,
        },
    )

    payload = json.loads(
        delegate_tool.delegate_task(
            tasks=[{"goal": "Do it"}],
            parent_agent=_parent(),
        )
    )

    assert built[0]._delegate_mode == "execute"
    assert built[0]._delegate_lane == "code_worker"
    assert payload["results"][0]["lane"] == "code_worker"


def test_batch_preflight_rejects_unusable_lane_before_build(monkeypatch):
    cfg = _config()
    cfg["lanes"]["smart_reviewer"]["toolsets"] = ["web"]
    parent = _parent()
    parent.enabled_toolsets = ["file"]
    build = MagicMock()

    monkeypatch.setattr(delegate_tool, "_load_config", lambda: cfg)
    monkeypatch.setattr(delegate_tool, "_build_child_agent", build)
    monkeypatch.setattr(
        delegate_tool,
        "_resolve_delegation_credentials_for_task",
        lambda config, _parent, task, *, resolved_lane: {
            "lane": resolved_lane,
            "provider": "provider",
            "model": "model",
            "base_url": "https://example.invalid/v1",
            "api_key": "key",
            "api_mode": "chat_completions",
            "toolsets": config["lanes"][resolved_lane]["toolsets"],
        },
    )

    payload = json.loads(
        delegate_tool.delegate_task(
            tasks=[
                {"goal": "Execute", "mode": "execute"},
                {"goal": "Review", "mode": "review"},
            ],
            parent_agent=parent,
        )
    )

    assert "no usable tools" in payload["error"].lower()
    assert len(payload["results"]) == 2
    assert [entry["task_index"] for entry in payload["results"]] == [0, 1]
    assert payload["results"][0]["status"] == "failed"
    assert payload["results"][0]["lane"] == "code_worker"
    assert payload["results"][0]["failure_kind"] == "batch_preflight_aborted"
    assert "task 1 failed validation" in payload["results"][0]["error"].lower()
    assert payload["results"][1]["status"] == "failed"
    assert payload["results"][1]["lane"] == "smart_reviewer"
    assert payload["results"][1]["failure_kind"] == "tool_execution_failed"
    for entry in payload["results"]:
        assert entry["evidence"] == {
            "tool_turn_count": 0,
            "successful_tool_count": 0,
        }
    build.assert_not_called()


def test_missing_execution_lane_returns_structured_failure_before_build(monkeypatch):
    build = MagicMock()
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {})
    monkeypatch.setattr(delegate_tool, "_build_child_agent", build)

    payload = json.loads(
        delegate_tool.delegate_task(
            tasks=[{"goal": "Execute"}],
            parent_agent=_parent(),
        )
    )

    assert "requires delegation.lanes.code_worker" in payload["error"]
    assert payload["results"] == [
        {
            "task_index": 0,
            "status": "failed",
            "summary": None,
            "error": payload["error"],
            "failure_kind": "provider_error",
            "mode": "execute",
            "lane": "code_worker",
            "provider": None,
            "routed_model": None,
            "evidence": {"tool_turn_count": 0, "successful_tool_count": 0},
        }
    ]
    build.assert_not_called()


@pytest.mark.parametrize(
    ("cfg", "message"),
    [
        (
            {
                "mode_routes": {
                    "execute": "code_worker",
                    "review": "smart_reviewer",
                    "reason": "local_worker",
                }
            },
            "mode_routes.*lanes is not configured",
        ),
        (
            {
                "local_first": {
                    "enabled": True,
                    "state_file": "/missing/controller.json",
                    "local_lane": "code_worker",
                    "degraded_lane": "smart_reviewer",
                    "max_state_age_seconds": 60,
                }
            },
            "requires delegation.lanes.code_worker",
        ),
        ({"lanes": {}}, "lanes must be a non-empty mapping"),
        ({"lanes": []}, "lanes must be a non-empty mapping"),
        ({"lanes": "code_worker"}, "lanes must be a non-empty mapping"),
    ],
)
def test_configured_route_policy_never_bypasses_parent_inheritance_validation(
    monkeypatch, cfg, message
):
    parent = _parent()
    parent.provider = "parent-provider"
    parent.model = "parent-model"
    parent.base_url = "https://parent.invalid/v1"
    build = MagicMock()
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: cfg)
    monkeypatch.setattr(delegate_tool, "_build_child_agent", build)

    payload = json.loads(
        delegate_tool.delegate_task(tasks=[{"goal": "Execute"}], parent_agent=parent)
    )

    assert payload["results"][0]["status"] == "failed"
    assert message.split(".*", 1)[0] in payload["error"]
    if ".*" in message:
        assert "lanes is not configured" in payload["error"]
    build.assert_not_called()


@pytest.mark.parametrize("policy", [{}, {"enabled": False}])
def test_disabled_local_first_preserves_legacy_parent_inheritance(
    monkeypatch, policy
):
    parent = _parent()
    parent.provider = "parent-provider"
    parent.model = "parent-model"
    child = MagicMock()
    child._delegate_role = "leaf"
    child.provider = "parent-provider"
    child.model = "parent-model"
    monkeypatch.setattr(
        delegate_tool, "_load_config", lambda: {"local_first": policy}
    )
    monkeypatch.setattr(delegate_tool, "_build_child_agent", lambda **_kwargs: child)
    monkeypatch.setattr(
        delegate_tool,
        "_run_single_child",
        lambda index, _goal, _child, _parent: _successful_entry(index),
    )

    payload = json.loads(
        delegate_tool.delegate_task(tasks=[{"goal": "Execute"}], parent_agent=parent)
    )

    assert payload["results"][0]["status"] == "completed"


def test_delegate_resolves_local_first_lane_once_for_action_and_receipt(monkeypatch):
    cfg = _config()
    resolve_lane = MagicMock(side_effect=["smart_reviewer", "code_worker"])
    built = []
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: cfg)
    monkeypatch.setattr(delegate_tool, "_resolve_lane_for_task", resolve_lane)
    monkeypatch.setattr(
        delegate_tool,
        "_resolve_delegation_credentials",
        lambda lane_cfg, _parent: {
            "provider": lane_cfg["provider"],
            "model": lane_cfg["model"],
            "base_url": None,
            "api_key": "key",
            "api_mode": "chat_completions",
        },
    )

    def fake_build(**kwargs):
        child = MagicMock()
        child._delegate_role = "leaf"
        child.provider = kwargs["override_provider"]
        child.model = kwargs["model"]
        built.append((child, kwargs))
        return child

    monkeypatch.setattr(delegate_tool, "_build_child_agent", fake_build)
    monkeypatch.setattr(
        delegate_tool,
        "_run_single_child",
        lambda index, _goal, child, _parent: {
            **_successful_entry(index),
            "lane": child._delegate_lane,
            "provider": child._delegate_provider,
            "routed_model": child._delegate_model,
        },
    )

    payload = json.loads(
        delegate_tool.delegate_task(tasks=[{"goal": "Execute"}], parent_agent=_parent())
    )

    assert resolve_lane.call_count == 1
    child, build_kwargs = built[0]
    assert build_kwargs["override_provider"] == "review-provider"
    assert build_kwargs["model"] == "review-model"
    assert child._delegate_lane == "smart_reviewer"
    assert payload["results"][0]["lane"] == "smart_reviewer"
    assert payload["results"][0]["provider"] == "review-provider"
    assert payload["results"][0]["routed_model"] == "review-model"


@pytest.mark.parametrize("status", ["completed", "error"])
def test_parent_inherited_runtime_is_preserved_in_stop_receipt(monkeypatch, status):
    parent = _parent()
    parent.provider = "parent-provider"
    parent.model = "parent-model"
    parent.base_url = "https://parent.invalid/v1"
    hook = MagicMock()
    child = MagicMock()
    child._delegate_role = "leaf"
    child.session_id = "child-parent-runtime"
    child.provider = "parent-provider"
    child.model = "parent-model"

    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {})
    monkeypatch.setattr(delegate_tool, "_build_child_agent", lambda **_kwargs: child)
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", hook)
    monkeypatch.setattr(
        delegate_tool,
        "_run_single_child",
        lambda index, _goal, _child, _parent: {
            **_successful_entry(index),
            "status": status,
            "summary": "done" if status == "completed" else None,
            "error": "child failed" if status == "error" else None,
            "exit_reason": status,
        },
    )

    json.loads(
        delegate_tool.delegate_task(tasks=[{"goal": "Execute"}], parent_agent=parent)
    )

    hook.assert_called_once()
    emitted = hook.call_args.kwargs
    assert emitted["child_status"] == status
    assert emitted["child_provider"] == "parent-provider"
    assert emitted["child_model"] == "parent-model"


def test_child_build_failure_preserves_selected_routing(monkeypatch):
    cfg = _config()
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: cfg)
    monkeypatch.setattr(
        delegate_tool,
        "_resolve_delegation_credentials_for_task",
        lambda config, parent, task, *, resolved_lane: {
            "provider": "custom:code",
            "model": "code-model",
            "base_url": "https://example.invalid/v1",
            "api_key": "key",
            "api_mode": "chat_completions",
            "toolsets": ["terminal", "file"],
        },
    )
    monkeypatch.setattr(
        delegate_tool,
        "_build_child_agent",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("provider init failed")),
    )

    payload = json.loads(
        delegate_tool.delegate_task(
            tasks=[{"goal": "Execute"}], parent_agent=_parent()
        )
    )

    assert payload["results"][0]["lane"] == "code_worker"
    assert payload["results"][0]["provider"] == "custom:code"
    assert payload["results"][0]["routed_model"] == "code-model"
    assert payload["results"][0]["failure_kind"] == "provider_error"


def test_later_child_build_failure_balances_built_child_lifecycle(monkeypatch):
    cfg = _config()
    parent = _parent()
    parent._active_children = []
    first_child = MagicMock()
    first_child.session_id = "child-1"
    first_child._delegate_role = "leaf"
    first_child.tool_progress_callback = MagicMock()
    hook = MagicMock()
    observe = MagicMock()
    calls = 0

    def fake_build(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second child init failed")
        parent._active_children.append(first_child)
        return first_child

    monkeypatch.setattr(delegate_tool, "_load_config", lambda: cfg)
    monkeypatch.setattr(
        delegate_tool,
        "_resolve_delegation_credentials_for_task",
        lambda config, _parent, task, *, resolved_lane: {
            "lane": resolved_lane,
            "provider": "custom:test",
            "model": "test-model",
            "base_url": "https://example.invalid/v1",
            "api_key": "key",
            "api_mode": "chat_completions",
            "toolsets": ["terminal", "file"],
        },
    )
    monkeypatch.setattr(delegate_tool, "_build_child_agent", fake_build)
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", hook)
    monkeypatch.setattr("hermes_cli.observability.observe_lifecycle", observe)

    payload = json.loads(
        delegate_tool.delegate_task(
            tasks=[{"goal": "First"}, {"goal": "Second"}],
            parent_agent=parent,
        )
    )

    assert len(payload["results"]) == 2
    assert payload["results"][0]["failure_kind"] == "batch_construction_aborted"
    assert "failed construction" in payload["results"][0]["error"].lower()
    assert parent._active_children == []
    first_child.tool_progress_callback.assert_called_once_with(
        "subagent.complete",
        preview="Batch aborted before execution",
        status="failed",
        duration_seconds=0,
        summary="",
    )
    first_child.close.assert_called_once_with()
    hook.assert_called_once()
    observe.assert_called_once()
    assert hook.call_args.args == ("subagent_stop",)
    emitted = hook.call_args.kwargs
    assert emitted["child_status"] == "failed"
    assert emitted["child_goal"] == "First"
    assert emitted["child_lane"] == "code_worker"
    assert emitted["child_provider"] == "custom:test"
    assert emitted["child_model"] == "test-model"
    assert emitted["child_mode"] == "execute"
    assert emitted["child_failure_kind"] == "batch_construction_aborted"
    assert emitted["child_exit_reason"] == "construction_aborted"
    assert emitted["child_successful_tool_count"] == 0
    assert emitted["tool_call_history"] == []


def test_parent_inherited_runtime_survives_later_child_construction_abort(monkeypatch):
    parent = _parent()
    parent.provider = "parent-provider"
    parent.model = "parent-model"
    parent.base_url = "https://parent.invalid/v1"
    parent._active_children = []
    first_child = MagicMock()
    first_child.session_id = "child-parent-runtime"
    first_child._delegate_role = "leaf"
    first_child.provider = "parent-provider"
    first_child.model = "parent-model"
    first_child.tool_progress_callback = MagicMock()
    hook = MagicMock()
    calls = 0

    def fake_build(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second child init failed")
        parent._active_children.append(first_child)
        return first_child

    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {})
    monkeypatch.setattr(delegate_tool, "_build_child_agent", fake_build)
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", hook)

    json.loads(
        delegate_tool.delegate_task(
            tasks=[{"goal": "First"}, {"goal": "Second"}],
            parent_agent=parent,
        )
    )

    hook.assert_called_once()
    emitted = hook.call_args.kwargs
    assert emitted["child_failure_kind"] == "batch_construction_aborted"
    assert emitted["child_provider"] == "parent-provider"
    assert emitted["child_model"] == "parent-model"
