from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from hermes_cli.subcommands.routing import classify_session_route
from hermes_state import SessionDB


_REPORT = {
    "main": {"provider": "custom:zeus", "model": "qwen3-coder-30b"},
    "delegation": {
        "fallback": {"provider": "neuralwatt", "model": "glm-5.2-fast"},
        "lanes": {
            "code_worker": {"provider": "neuralwatt", "model": "kimi-k2.7-code"},
        },
    },
}


def test_classify_session_route_distinguishes_main_delegation_and_unknown():
    main = classify_session_route(
        {"source": "cli", "billing_provider": "custom", "model": "qwen3-coder-30b"},
        _REPORT,
    )
    delegated = classify_session_route(
        {
            "source": "subagent",
            "parent_session_id": "parent",
            "billing_provider": "neuralwatt",
            "model": "kimi-k2.7-code",
        },
        _REPORT,
    )
    unexplained = classify_session_route(
        {"source": "cli", "billing_provider": "openai-codex", "model": "gpt-5.6"},
        _REPORT,
    )
    missing = classify_session_route(
        {"source": "cli", "billing_provider": None, "model": "qwen3-coder-30b"},
        _REPORT,
    )

    assert main["reason_code"] == "matches_main"
    assert delegated["reason_code"] == "matches_delegation"
    assert unexplained["reason_code"] == "unexplained"
    assert "explicit overrides" in unexplained["note"]
    assert missing["reason_code"] == "unknown_runtime"


def _run(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "routing", *args],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )


def test_routing_explain_and_audit_cli_are_read_only_and_json_safe(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    db = SessionDB(db_path=home / "state.db")
    db.create_session("main-session", "cli", model="qwen3-coder-30b")
    db.update_token_counts(
        "main-session",
        input_tokens=1,
        output_tokens=1,
        billing_provider="custom",
        billing_base_url="http://127.0.0.1:8080/v1",
    )
    db.close()

    explained = _run(home, "explain", "main-session", "--json")
    assert explained.returncode == 0, explained.stderr + explained.stdout
    explanation = json.loads(explained.stdout)
    assert explanation["session_id"] == "main-session"
    assert "billing_base_url" not in explanation
    assert "api_key" not in explained.stdout.lower()

    audited = _run(home, "audit", "--days", "1", "--json")
    assert audited.returncode == 0, audited.stderr + audited.stdout
    report = json.loads(audited.stdout)
    assert report["policy"] == "audit_only"
    assert report["audited_sessions"] == 1
    assert report["sessions"][0]["session_id"] == "main-session"
    assert "127.0.0.1" not in audited.stdout


def test_routing_explain_unknown_session_fails(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    SessionDB(db_path=home / "state.db").close()
    result = _run(home, "explain", "missing")
    assert result.returncode != 0
    assert "No persisted session found" in result.stderr
