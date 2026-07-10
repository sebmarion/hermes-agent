"""Regression tests for delegate_task per-task lane routing."""

import pytest

from tools import delegate_tool


def _lane_cfg():
    return {
        "provider": "global-provider",
        "model": "global-model",
        "lanes": {
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


def test_default_lane_applies_when_task_has_no_route_or_tier():
    cfg = _lane_cfg()

    lane = delegate_tool._resolve_lane_for_task({}, cfg)

    assert lane == "local_worker"


def test_missing_lanes_falls_back_to_global_delegation_config():
    cfg = {"provider": "global-provider", "model": "global-model"}

    lane = delegate_tool._resolve_lane_for_task({"model_tier": "micro"}, cfg)

    assert lane is None


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
    ("raw", "expected"),
    [
        (" terminal, file , ", ["terminal", "file"]),
        (["terminal", " file ", ""], ["terminal", "file"]),
        (None, None),
        (42, None),
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
        cfg, parent_agent=object(), task={"model_tier": "micro"}
    )

    assert calls == [cfg]
    assert creds == {
        "model": "global-model",
        "provider": "global-provider",
        "base_url": None,
        "api_key": "test-key",
        "api_mode": None,
    }
