"""Regression tests for delegate_task per-task lane routing."""

import json
from unittest.mock import MagicMock

import pytest

from tools import delegate_tool


def _lane_cfg():
    return {
        "provider": "global-provider",
        "model": "global-model",
        "lanes": {
            "code_worker": {
                "provider": "custom:code",
                "model": "code-model",
                "toolsets": ["terminal", "file"],
            },
            "local_worker": {
                "provider": "custom:zeus",
                "model": "qwopus-30b-coder",
                "toolsets": ["terminal", "file"],
            },
            "smart_reviewer": {
                "provider": "neuralwatt",
                "model": "glm-5.2",
                "toolsets": ["file"],
            },
        },
        "tier_routes": {
            "micro": "local_worker",
            "large": "smart_reviewer",
            "review": "smart_reviewer",
        },
        "default_lane": "local_worker",
    }


def test_explicit_route_beats_model_tier_and_default_lane():
    cfg = _lane_cfg()

    lane = delegate_tool._resolve_lane_for_task(
        {"route": "smart_reviewer", "model_tier": "micro"}, cfg
    )

    assert lane == "smart_reviewer"


def test_model_tier_maps_to_configured_lane():
    cfg = _lane_cfg()

    lane = delegate_tool._resolve_lane_for_task({"model_tier": "micro"}, cfg)

    assert lane == "local_worker"


def test_execute_mode_applies_when_task_has_no_route_or_tier():
    cfg = _lane_cfg()

    lane = delegate_tool._resolve_lane_for_task({}, cfg)

    assert lane == "code_worker"


def test_explicit_global_provider_model_is_treated_as_code_worker_compat_lane():
    cfg = {"provider": "global-provider", "model": "global-model"}

    lane = delegate_tool._resolve_lane_for_task({}, cfg)

    assert lane == "code_worker"


def test_missing_lanes_and_explicit_delegate_runtime_fails_closed():
    with pytest.raises(ValueError, match="requires delegation.lanes.code_worker"):
        delegate_tool._resolve_lane_for_task({}, {})


@pytest.mark.parametrize("invalid_lanes", [[], "", "worker", 42, {}])
def test_explicit_empty_or_malformed_lanes_config_fails_closed(invalid_lanes):
    cfg = {
        "provider": "global-provider",
        "model": "global-model",
        "lanes": invalid_lanes,
    }

    with pytest.raises(ValueError, match="lanes must be a non-empty mapping"):
        delegate_tool._resolve_lane_for_task({}, cfg)


def test_legacy_default_lane_without_lanes_does_not_override_code_worker_compat():
    cfg = {
        "provider": "global-provider",
        "model": "global-model",
        "default_lane": "worker",
    }

    assert delegate_tool._resolve_lane_for_task({}, cfg) == "code_worker"


@pytest.mark.parametrize("bad_routes", [[], "large:worker", 42, None])
def test_tier_routes_without_lanes_validate_mapping_shape(bad_routes):
    cfg = {
        "provider": "global-provider",
        "model": "global-model",
        "tier_routes": bad_routes,
    }

    with pytest.raises(ValueError, match="tier_routes must be a mapping"):
        delegate_tool._resolve_lane_for_task({}, cfg)


@pytest.mark.parametrize(
    "bad_routes",
    [
        {"": "worker"},
        {" large ": "worker"},
        {"large": " worker "},
        {42: "worker"},
        {"large": 42},
    ],
)
def test_tier_routes_without_lanes_validate_members(bad_routes):
    cfg = {
        "provider": "global-provider",
        "model": "global-model",
        "tier_routes": bad_routes,
    }

    with pytest.raises(ValueError, match="tier_routes keys and values.*trimmed strings"):
        delegate_tool._resolve_lane_for_task({}, cfg)


@pytest.mark.parametrize("routes", [{}, {"large": "worker"}])
def test_tier_routes_without_lanes_never_fall_back_globally(routes):
    cfg = {
        "provider": "global-provider",
        "model": "global-model",
        "tier_routes": routes,
    }

    with pytest.raises(ValueError, match="tier_routes.*lanes is not configured"):
        delegate_tool._resolve_lane_for_task({}, cfg)


@pytest.mark.parametrize(
    ("task", "message"),
    [
        ({"route": "smart_reviewer"}, "route 'smart_reviewer'.*lanes is not configured"),
        ({"model_tier": "large"}, "model_tier 'large'.*lanes is not configured"),
    ],
)
def test_selector_without_lanes_fails_closed(task, message):
    cfg = {"provider": "global-provider", "model": "global-model"}

    with pytest.raises(ValueError, match=message):
        delegate_tool._resolve_lane_for_task(task, cfg)


def test_unknown_explicit_route_fails_closed_with_available_lanes():
    cfg = _lane_cfg()

    with pytest.raises(ValueError, match="Task route 'missing' not found") as exc:
        delegate_tool._resolve_lane_for_task({"route": "missing"}, cfg)

    assert "local_worker" in str(exc.value)
    assert "smart_reviewer" in str(exc.value)


def test_tier_mapping_to_missing_lane_fails_closed():
    cfg = _lane_cfg()
    cfg["tier_routes"]["security"] = "ghost_lane"

    with pytest.raises(ValueError, match="Tier 'security' maps to lane 'ghost_lane'"):
        delegate_tool._resolve_lane_for_task({"model_tier": "security"}, cfg)


def test_stale_default_lane_is_ignored_by_mode_routing():
    cfg = _lane_cfg()
    cfg["default_lane"] = "ghost_lane"

    assert delegate_tool._resolve_lane_for_task({}, cfg) == "code_worker"


def test_lanes_without_default_use_execute_mode():
    cfg = _lane_cfg()
    del cfg["default_lane"]

    assert delegate_tool._resolve_lane_for_task({}, cfg) == "code_worker"


def test_unmapped_tier_fails_closed_without_mode_fallback():
    cfg = _lane_cfg()
    del cfg["default_lane"]

    with pytest.raises(ValueError, match="model_tier 'unmapped'.*not mapped"):
        delegate_tool._resolve_lane_for_task({"model_tier": "unmapped"}, cfg)


@pytest.mark.parametrize("bad_definition", [{}, [], "worker", None, 42])
def test_malformed_lane_definition_fails_closed(bad_definition):
    cfg = _lane_cfg()
    cfg["lanes"]["broken"] = bad_definition

    with pytest.raises(ValueError, match="lane 'broken'.*non-empty mapping"):
        delegate_tool._resolve_lane_for_task({}, cfg)


@pytest.mark.parametrize(
    ("lane_definition", "missing_key"),
    [
        ({"model": "model-only"}, "provider"),
        ({"provider": "provider-only"}, "model"),
        ({"provider": "", "model": "model"}, "provider"),
        ({"provider": "provider", "model": None}, "model"),
    ],
)
def test_lane_requires_explicit_provider_and_model(lane_definition, missing_key):
    cfg = _lane_cfg()
    cfg["lanes"]["broken"] = lane_definition

    with pytest.raises(ValueError, match=f"lane 'broken'.*non-empty {missing_key}"):
        delegate_tool._resolve_lane_for_task({}, cfg)


@pytest.mark.parametrize("bad_name", ["", " worker ", 42, None])
def test_lane_names_must_be_non_empty_trimmed_strings(bad_name):
    cfg = _lane_cfg()
    cfg["lanes"][bad_name] = {
        "provider": "provider",
        "model": "model",
        "toolsets": ["file"],
    }

    with pytest.raises(ValueError, match="lanes keys must be non-empty trimmed strings"):
        delegate_tool._resolve_lane_for_task({}, cfg)


@pytest.mark.parametrize("bad_routes", [[], "large:worker", 42, None])
def test_malformed_tier_routes_fail_closed(bad_routes):
    cfg = _lane_cfg()
    cfg["tier_routes"] = bad_routes

    with pytest.raises(ValueError, match="tier_routes must be a mapping"):
        delegate_tool._resolve_lane_for_task({}, cfg)


@pytest.mark.parametrize(
    "bad_routes",
    [
        {"": "local_worker"},
        {" large ": "local_worker"},
        {"large": " local_worker "},
        {42: "local_worker"},
        {"large": 42},
    ],
)
def test_tier_route_members_must_be_non_empty_trimmed_strings(bad_routes):
    cfg = _lane_cfg()
    cfg["tier_routes"] = bad_routes

    with pytest.raises(ValueError, match="tier_routes keys and values.*trimmed strings"):
        delegate_tool._resolve_lane_for_task({}, cfg)


def test_lane_credentials_and_toolsets_are_resolved_per_task(monkeypatch):
    calls = []

    def fake_resolve_credentials(cfg, parent_agent):
        calls.append(cfg)
        return {
            "model": cfg.get("model"),
            "provider": cfg.get("provider"),
            "base_url": cfg.get("base_url"),
            "api_key": "test-key",
            "api_mode": cfg.get("api_mode"),
        }

    monkeypatch.setattr(
        delegate_tool,
        "_resolve_delegation_credentials",
        fake_resolve_credentials,
    )

    creds = delegate_tool._resolve_delegation_credentials_for_task(
        _lane_cfg(), parent_agent=object(), task={"model_tier": "large"}
    )

    assert calls == [_lane_cfg()["lanes"]["smart_reviewer"]]
    assert creds == {
        "model": "glm-5.2",
        "provider": "neuralwatt",
        "base_url": None,
        "api_key": "test-key",
        "api_mode": None,
        "toolsets": ["file"],
        "lane": "smart_reviewer",
    }


@pytest.mark.parametrize(
    "invalid_toolsets",
    [[], "", " , ", 42, [42], [None], [{}], ["missing_toolset"]],
)
def test_explicit_empty_or_invalid_lane_toolsets_fail_closed(monkeypatch, invalid_toolsets):
    cfg = _lane_cfg()
    cfg["lanes"]["smart_reviewer"]["toolsets"] = invalid_toolsets
    monkeypatch.setattr(
        delegate_tool,
        "_resolve_delegation_credentials",
        lambda lane_cfg, parent_agent: {
            "model": lane_cfg.get("model"),
            "provider": lane_cfg.get("provider"),
            "base_url": None,
            "api_key": "test-key",
            "api_mode": None,
        },
    )

    with pytest.raises(ValueError, match="lane 'smart_reviewer'.*invalid toolsets"):
        delegate_tool._resolve_delegation_credentials_for_task(
            cfg, parent_agent=object(), task={"route": "smart_reviewer"}
        )


def test_lane_without_toolsets_inherits_parent_by_omission(monkeypatch):
    cfg = _lane_cfg()
    del cfg["lanes"]["smart_reviewer"]["toolsets"]
    monkeypatch.setattr(
        delegate_tool,
        "_resolve_delegation_credentials",
        lambda lane_cfg, parent_agent: {
            "model": lane_cfg.get("model"),
            "provider": lane_cfg.get("provider"),
            "base_url": None,
            "api_key": "test-key",
            "api_mode": None,
        },
    )

    creds = delegate_tool._resolve_delegation_credentials_for_task(
        cfg, parent_agent=object(), task={"route": "smart_reviewer"}
    )

    assert "toolsets" not in creds
    assert creds["lane"] == "smart_reviewer"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" terminal, file , ", ["terminal", "file"]),
        (["terminal", " file ", ""], ["terminal", "file"]),
        (None, None),
        (42, None),
        ([42], None),
        ([None], None),
        ([{}], None),
        (["missing_toolset"], None),
    ],
)
def test_lane_toolsets_normalize_cli_scalars_and_sequences(raw, expected):
    assert delegate_tool._normalize_lane_toolsets(raw) == expected


def test_async_batch_model_label_is_truthful_for_lane_batches():
    assert delegate_tool._async_batch_model_label([]) is None
    assert delegate_tool._async_batch_model_label([None, "glm-5.2", "glm-5.2"]) == "glm-5.2"
    assert delegate_tool._async_batch_model_label(
        ["glm-5.2", "kimi-k2.7-code", "glm-5.2"]
    ) == "mixed:glm-5.2,kimi-k2.7-code"


def test_credentials_fall_back_to_global_when_no_lane_resolves(monkeypatch):
    calls = []
    cfg = {"provider": "global-provider", "model": "global-model"}

    def fake_resolve_credentials(cfg, parent_agent):
        calls.append(cfg)
        return {
            "model": cfg.get("model"),
            "provider": cfg.get("provider"),
            "base_url": None,
            "api_key": "test-key",
            "api_mode": None,
        }

    monkeypatch.setattr(
        delegate_tool,
        "_resolve_delegation_credentials",
        fake_resolve_credentials,
    )

    creds = delegate_tool._resolve_delegation_credentials_for_task(
        cfg, parent_agent=object(), task={}
    )

    assert calls == [cfg]
    assert creds == {
        "model": "global-model",
        "provider": "global-provider",
        "base_url": None,
        "api_key": "test-key",
        "api_mode": None,
        "lane": "code_worker",
    }


def test_explicit_lane_disables_parent_fallback_when_building_child(monkeypatch):
    cfg = _lane_cfg()
    parent = MagicMock()
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = None
    parent.enabled_toolsets = ["terminal", "file", "web", "delegation"]
    parent.valid_tool_names = {"terminal", "read_file", "write_file", "delegate_task"}
    parent.tool_progress_callback = None
    parent._session_db = None
    parent._memory_manager = None
    parent.session_estimated_cost_usd = 0.0

    monkeypatch.setattr(delegate_tool, "_load_config", lambda: cfg)
    monkeypatch.setattr(
        delegate_tool,
        "_resolve_delegation_credentials_for_task",
        lambda _cfg, _parent, _task: {
            "model": "glm-5.2",
            "provider": "neuralwatt",
            "base_url": "https://lane.example/v1",
            "api_key": "test-key",
            "api_mode": "chat_completions",
            "toolsets": ["file"],
            "lane": "smart_reviewer",
        },
    )
    captured = {}
    child = MagicMock()
    child._delegate_role = "leaf"

    def fake_build_child_agent(**kwargs):
        captured.update(kwargs)
        return child

    monkeypatch.setattr(delegate_tool, "_build_child_agent", fake_build_child_agent)
    monkeypatch.setattr(
        delegate_tool,
        "_run_single_child",
        lambda *_args, **_kwargs: {
            "task_index": 0,
            "status": "completed",
            "summary": "reviewed",
            "api_calls": 1,
            "duration_seconds": 0.01,
            "_child_role": "leaf",
        },
    )

    result = json.loads(
        delegate_tool.delegate_task(
            tasks=[{"goal": "Review the repository", "route": "smart_reviewer"}],
            parent_agent=parent,
        )
    )

    assert result["results"][0]["status"] == "completed"
    assert captured["model"] == "glm-5.2"
    assert captured["inherit_parent_fallback"] is False
