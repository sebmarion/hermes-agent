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
        lambda config, parent, task: {
            "lane": delegate_tool._resolve_lane_for_task(task, config),
            "provider": "provider",
            "model": "model",
            "base_url": "https://example.invalid/v1",
            "api_key": "key",
            "api_mode": "chat_completions",
            "toolsets": config["lanes"][delegate_tool._resolve_lane_for_task(task, config)]["toolsets"],
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
        lambda config, _parent, task: {
            "lane": delegate_tool._resolve_lane_for_task(task, config),
            "provider": "provider",
            "model": "model",
            "base_url": "https://example.invalid/v1",
            "api_key": "key",
            "api_mode": "chat_completions",
            "toolsets": config["lanes"][delegate_tool._resolve_lane_for_task(task, config)]["toolsets"],
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


def test_child_build_failure_preserves_selected_routing(monkeypatch):
    cfg = _config()
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: cfg)
    monkeypatch.setattr(
        delegate_tool,
        "_resolve_delegation_credentials_for_task",
        lambda config, parent, task: {
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
        lambda config, _parent, task: {
            "lane": delegate_tool._resolve_lane_for_task(task, config),
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

    payload = json.loads(
        delegate_tool.delegate_task(
            tasks=[{"goal": "First"}, {"goal": "Second"}],
            parent_agent=parent,
        )
    )

    assert len(payload["results"]) == 2
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
    assert hook.call_args.args == ("subagent_stop",)
    assert hook.call_args.kwargs["child_status"] == "failed"
