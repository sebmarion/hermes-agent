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
    build.assert_not_called()
