from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli.subcommands.routing import build_routing_audit, classify_session_route
from hermes_state import SessionDB


_ROUTES = {
    "main": {"provider": "custom:zeus", "model": "qwen3-coder-30b"},
    "delegation": {"provider": "neuralwatt", "model": "kimi-k2.7-code"},
}


def test_classify_session_route_uses_only_persisted_facts_and_configured_routes():
    main = classify_session_route(
        {"source": "cli", "billing_provider": "custom", "model": "qwen3-coder-30b"},
        _ROUTES,
    )
    delegated = classify_session_route(
        {
            "source": "cli",
            "parent_session_id": "parent",
            "model_config": json.dumps({"_delegate_from": "parent"}),
            "billing_provider": "neuralwatt",
            "model": "kimi-k2.7-code",
        },
        _ROUTES,
    )
    unexplained = classify_session_route(
        {"source": "cli", "billing_provider": "openai-codex", "model": "gpt-5.6"},
        _ROUTES,
    )
    unknown_runtime = classify_session_route(
        {"source": "cli", "billing_provider": None, "model": "qwen3-coder-30b"},
        _ROUTES,
    )
    unknown_config = classify_session_route(
        {"source": "cli", "billing_provider": "custom", "model": "qwen3-coder-30b"},
        {"main": {"provider": None, "model": "qwen3-coder-30b"}},
    )

    assert main["reason_code"] == "matches_main"
    assert delegated["reason_code"] == "matches_delegation"
    assert unexplained["reason_code"] == "unexplained"
    assert "not persisted" in unexplained["note"]
    assert unknown_runtime["reason_code"] == "unknown_runtime"
    assert unknown_config["reason_code"] == "unknown_configuration"


def test_classify_delegated_session_matches_any_configured_lane_without_inventing_lane():
    routes = {
        "main": {"provider": "custom:zeus", "model": "qwen3-coder-30b"},
        "delegation": {
            "complete": True,
            "candidates": [
                {"provider": "neuralwatt", "model": "kimi-k2.7-code"},
                {"provider": "openai-codex", "model": "gpt-5.6"},
            ],
        },
    }
    session = {
        "source": "cli",
        "model_config": json.dumps({"_delegate_from": "parent"}),
        "billing_provider": "openai-codex",
        "model": "gpt-5.6",
    }

    explained = classify_session_route(session, routes)

    assert explained["reason_code"] == "matches_delegation"
    assert "lane identity was not persisted" in explained["note"]
    assert explained["expected_route"] == {
        "provider": "openai-codex",
        "model": "gpt-5.6",
    }


def _run_routing(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(root)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "routing", *args],
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )


def _seed_routing_home(home: Path) -> None:
    home.mkdir()
    (home / "config.yaml").write_text(
        """\
model:
  provider: custom:zeus
  default: qwen3-coder-30b
  base_url: http://127.0.0.1:8080/v1
  api_key: do-not-print-main-key
delegation:
  provider: neuralwatt
  model: kimi-k2.7-code
  base_url: https://private.example/v1
  api_key: do-not-print-delegation-key
""",
        encoding="utf-8",
    )
    db = SessionDB(db_path=home / "state.db")
    db.create_session("main-session", "cli", model="qwen3-coder-30b")
    db.update_token_counts(
        "main-session",
        input_tokens=1,
        output_tokens=1,
        billing_provider="custom",
        billing_base_url="http://127.0.0.1:8080/v1",
    )
    db.create_session(
        "delegate-session",
        "cli",
        model="kimi-k2.7-code",
        parent_session_id="main-session",
        model_config={"_delegate_from": "main-session"},
    )
    db.update_token_counts(
        "delegate-session",
        input_tokens=1,
        output_tokens=1,
        billing_provider="neuralwatt",
        billing_base_url="https://private.example/v1",
    )
    db.close()


def test_routing_explain_and_audit_cli_are_read_only_and_credential_free(tmp_path):
    home = tmp_path / "home"
    _seed_routing_home(home)

    before_config = (home / "config.yaml").read_bytes()
    before_db_mtime = (home / "state.db").stat().st_mtime_ns
    explained = _run_routing(home, "explain", "main-session", "--json")
    assert explained.returncode == 0, explained.stderr + explained.stdout
    explanation = json.loads(explained.stdout)
    assert explanation["session_id"] == "main-session"
    assert explanation["reason_code"] == "matches_main"
    assert "billing_base_url" not in explanation

    audited = _run_routing(home, "audit", "--days", "1", "--json")
    assert audited.returncode == 0, audited.stderr + audited.stdout
    report = json.loads(audited.stdout)
    assert report["policy"] == "audit_only"
    assert report["audited_sessions"] == 2
    assert report["classifications"] == {
        "matches_delegation": 1,
        "matches_main": 1,
    }
    assert {row["session_id"] for row in report["sessions"]} == {
        "main-session",
        "delegate-session",
    }

    combined = explained.stdout + explained.stderr + audited.stdout + audited.stderr
    assert "do-not-print" not in combined
    assert "127.0.0.1" not in combined
    assert "private.example" not in combined
    assert "api_key" not in combined.lower()
    assert (home / "config.yaml").read_bytes() == before_config
    assert (home / "state.db").stat().st_mtime_ns == before_db_mtime


def test_routing_explain_unknown_session_fails_nonzero(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    SessionDB(db_path=home / "state.db").close()

    result = _run_routing(home, "explain", "missing")

    assert result.returncode != 0
    assert "No persisted session found" in result.stderr


def test_routing_explain_matches_one_of_the_profile_delegation_lanes(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        """\
model:
  provider: custom:zeus
  default: qwen3-coder-30b
delegation:
  lanes:
    code_worker:
      provider: neuralwatt
      model: kimi-k2.7-code
      api_key: do-not-print-worker-key
    smart_reviewer:
      provider: openai-codex
      model: gpt-5.6
      api_key: do-not-print-review-key
""",
        encoding="utf-8",
    )
    db = SessionDB(db_path=home / "state.db")
    db.create_session("main-session", "cli", model="qwen3-coder-30b")
    db.create_session(
        "lane-session",
        "cli",
        model="gpt-5.6",
        parent_session_id="main-session",
        model_config={"_delegate_from": "main-session"},
    )
    db.update_token_counts(
        "lane-session",
        input_tokens=1,
        output_tokens=1,
        billing_provider="openai-codex",
    )
    db.close()

    explained = _run_routing(home, "explain", "lane-session", "--json")

    assert explained.returncode == 0, explained.stderr + explained.stdout
    payload = json.loads(explained.stdout)
    assert payload["reason_code"] == "matches_delegation"
    assert payload["expected_route"] == {
        "provider": "openai-codex",
        "model": "gpt-5.6",
    }
    assert "lane identity was not persisted" in payload["note"]
    assert "do-not-print" not in explained.stdout + explained.stderr


def test_routing_audit_reports_when_the_requested_limit_truncates_results(tmp_path):
    home = tmp_path / "home"
    _seed_routing_home(home)
    db = SessionDB(db_path=home / "state.db")
    db.create_session("third-session", "cli", model="qwen3-coder-30b")
    db.update_token_counts(
        "third-session",
        input_tokens=1,
        output_tokens=1,
        billing_provider="custom",
    )
    db.close()

    audited = _run_routing(home, "audit", "--days", "1", "--limit", "2", "--json")

    assert audited.returncode == 0, audited.stderr + audited.stdout
    report = json.loads(audited.stdout)
    assert report["audited_sessions"] == 2
    assert report["complete"] is False
    assert report["truncated"] is True
    assert report["limit"] == 2


@pytest.mark.parametrize("days", [0, -1, float("nan"), float("inf")])
def test_routing_audit_rejects_invalid_windows(tmp_path, monkeypatch, days):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    SessionDB(db_path=home / "state.db").close()

    with pytest.raises(ValueError, match="days must be positive and finite"):
        build_routing_audit(days=days)
