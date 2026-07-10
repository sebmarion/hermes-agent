"""Shadow autonomy router contract tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import autonomy_shadow as shadow


def _valid_plan() -> dict:
    return {
        "version": 1,
        "mode": "direct",
        "risk": "low",
        "slices": [{
            "id": "work",
            "kind": "implement",
            "goal": "Implement the requested change",
            "depends_on": [],
            "capability": "local_execution",
            "workspace": ".",
            "allowed_paths": ["src"],
            "read_only": False,
            "expected_artifacts": ["changed source"],
            "acceptance": ["focused tests pass"],
        }],
        "merge_policy": "single slice",
        "stop_condition": "focused tests pass",
        "escalation_predicates": [],
    }


def test_disabled_shadow_does_no_work(monkeypatch, tmp_path):
    monkeypatch.setattr(shadow, "load_config", lambda: {"autonomy": {"enabled": False}})
    monkeypatch.setattr(shadow, "get_hermes_home", lambda: tmp_path)
    called = []
    monkeypatch.setattr(shadow, "_plan_turn", lambda *a, **k: called.append(True))

    assert shadow.observe_turn("do work", session_id="s", source="cli") is None
    assert called == []
    assert not (tmp_path / "autonomy" / "shadow-observations.jsonl").exists()


def test_non_shadow_mode_is_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(
        shadow,
        "load_config",
        lambda: {"autonomy": {"enabled": True, "mode": "execute"}},
    )
    monkeypatch.setattr(shadow, "get_hermes_home", lambda: tmp_path)

    result = shadow.observe_turn("do work", session_id="s", source="cli")

    assert result is not None
    assert result["status"] == "policy_rejected"
    assert result["effective_mode"] == "shadow"


def test_enabled_shadow_compiles_and_writes_minimal_observation(monkeypatch, tmp_path):
    monkeypatch.setattr(
        shadow,
        "load_config",
        lambda: {"autonomy": {"enabled": True, "mode": "shadow"}},
    )
    monkeypatch.setattr(shadow, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(shadow, "_plan_turn", lambda *a, **k: _valid_plan())

    result = shadow.observe_turn(
        "secret user request",
        session_id="session-1",
        source="gateway:telegram",
        workspace="/repo",
    )

    assert result is not None
    assert result["status"] == "accepted"
    assert result["plan"]["mode"] == "direct"
    path = tmp_path / "autonomy" / "shadow-observations.jsonl"
    row = json.loads(path.read_text().strip())
    assert row["session_id"] == "session-1"
    assert row["source"] == "gateway:telegram"
    assert row["workspace_name"] == "repo"
    assert "/repo" not in path.read_text()
    assert row["prompt_chars"] == len("secret user request")
    assert "secret user request" not in path.read_text()
    assert "prompt" not in row
    assert row["plan"]["slice_count"] == 1


def test_invalid_plan_is_observed_without_raising(monkeypatch, tmp_path):
    monkeypatch.setattr(
        shadow,
        "load_config",
        lambda: {"autonomy": {"enabled": True, "mode": "shadow"}},
    )
    monkeypatch.setattr(shadow, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(shadow, "_plan_turn", lambda *a, **k: {"bad": True})

    result = shadow.observe_turn("do work", session_id="s", source="cli")

    assert result is not None
    assert result["status"] == "invalid_plan"
    assert "error" in result


def test_submit_is_non_blocking_and_deduplicates_same_turn(monkeypatch):
    monkeypatch.setattr(
        shadow,
        "load_config",
        lambda: {"autonomy": {"enabled": True, "mode": "shadow"}},
    )
    submitted = []
    monkeypatch.setattr(
        shadow,
        "_executor",
        SimpleNamespace(submit=lambda fn, *args, **kwargs: submitted.append((fn, args, kwargs))),
    )
    shadow._recent_turns.clear()

    first = shadow.submit_shadow_observation("same", session_id="s", source="cli")
    second = shadow.submit_shadow_observation("same", session_id="s", source="cli")

    assert first is True
    assert second is False
    assert len(submitted) == 1


def test_submit_failure_releases_dedupe_claim(monkeypatch):
    monkeypatch.setattr(
        shadow,
        "load_config",
        lambda: {"autonomy": {"enabled": True, "mode": "shadow"}},
    )

    def fail_submit(*args, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(shadow, "_executor", SimpleNamespace(submit=fail_submit))
    shadow._recent_turns.clear()

    assert shadow.submit_shadow_observation("retry", session_id="s", source="cli") is False
    assert shadow.submit_shadow_observation("retry", session_id="s", source="cli") is False
    assert shadow._recent_turns == {}


def test_lane_assignments_prefer_named_capability_lanes():
    plan = shadow.compile_execution_plan(_valid_plan())
    config = {
        "delegation": {
            "default_lane": "local_worker",
            "lanes": {
                "local_worker": {},
                "code_worker": {},
                "smart_reviewer": {},
            },
        }
    }

    receipt = shadow._plan_receipt(plan, config)

    assert receipt["lane_assignments"] == [
        {"slice_id": "work", "capability": "local_execution", "lane": "local_worker"}
    ]


def test_lane_assignment_is_null_when_no_lane_is_configured():
    plan = shadow.compile_execution_plan(_valid_plan())

    receipt = shadow._plan_receipt(plan, {"delegation": {}})

    assert receipt["lane_assignments"][0]["lane"] is None


def test_cli_and_gateway_have_fail_open_shadow_ingress():
    root = Path(__file__).resolve().parents[2]
    cli_source = (root / "cli.py").read_text()
    gateway_source = (root / "gateway" / "run.py").read_text()

    assert "submit_shadow_observation(" in cli_source
    assert 'source="cli"' in cli_source
    assert "autonomy shadow ingress failed open" in cli_source
    assert "submit_shadow_observation(" in gateway_source
    assert 'source=f"gateway:{source.platform}"' in gateway_source
    assert "autonomy shadow ingress failed open" in gateway_source


def test_planner_requests_strict_json_schema(monkeypatch):
    captured = {}

    class Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(_valid_plan())))]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    monkeypatch.setattr(shadow, "get_text_auxiliary_client", lambda task: (client, "local"))

    result = shadow._plan_turn("do work", workspace="/repo", timeout=9)

    assert result == _valid_plan()
    schema = captured["response_format"]["json_schema"]
    assert schema["strict"] is True
    assert schema["schema"]["additionalProperties"] is False
    assert captured["timeout"] == 9


def test_planner_rejects_native_adapter_that_drops_strict_schema(monkeypatch):
    class AnthropicAuxiliaryClient:
        pass

    monkeypatch.setattr(
        shadow,
        "get_text_auxiliary_client",
        lambda task: (AnthropicAuxiliaryClient(), "model"),
    )

    with pytest.raises(RuntimeError, match="strict json_schema"):
        shadow._plan_turn("do work", workspace="/repo", timeout=9)
