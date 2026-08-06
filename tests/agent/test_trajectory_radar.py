from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from agent.trajectory_radar import TrajectoryRadar, render_markdown
from hermes_state import SessionDB


def _seed_db(path: Path) -> SessionDB:
    db = SessionDB(db_path=path)
    now = time.time()

    db.create_session("s-verify", "webui", model="gpt-5.5", cwd="/tmp/hermes")
    db.update_token_counts(
        "s-verify",
        input_tokens=1000,
        output_tokens=100,
        billing_provider="openai-codex",
        billing_base_url="https://example.invalid/v1",
    )
    db.append_message("s-verify", "user", "Did you check that? Are we done?", timestamp=now - 30)
    db.append_message("s-verify", "assistant", "Done.", timestamp=now - 29)

    db.create_session("s-routing", "cli", model="glm-5.2", cwd="/tmp/hermes")
    db.update_token_counts(
        "s-routing",
        input_tokens=2000,
        output_tokens=200,
        billing_provider="neuralwatt",
        billing_base_url="https://api.neuralwatt.example/v1",
    )
    db.append_message("s-routing", "user", "Why did this use the cloud provider instead of local Zeus routing?", timestamp=now - 20)

    db.create_session("s-secret", "cli", model="qwen3-coder-30b", cwd="/tmp/hermes")
    db.append_message("s-secret", "user", "Did you check token Bearer abcdefghijklmnopqrstuvwxyz should not leak", timestamp=now - 10)
    return db


def test_trajectory_radar_generates_privacy_preserving_candidates(tmp_path):
    db = _seed_db(tmp_path / "state.db")
    try:
        report = TrajectoryRadar(db).generate(days=1, limit=5)
    finally:
        db.close()

    assert report["totals"]["sessions"] == 3
    ids = {candidate["id"] for candidate in report["candidates"]}
    assert "done-means-proven-gatekeeper" in ids
    assert "local-first-dispatch-firewall" in ids

    rendered_json = json.dumps(report)
    assert "Bearer abcdefghijklmnopqrstuvwxyz" not in rendered_json
    assert "Did you check that" not in rendered_json
    assert "message_id" in rendered_json


def test_trajectory_radar_include_snippets_redacts_secretish_content(tmp_path):
    db = _seed_db(tmp_path / "state.db")
    try:
        report = TrajectoryRadar(db).generate(days=1, limit=10, include_snippets=True)
    finally:
        db.close()

    rendered_json = json.dumps(report)
    assert "[redacted: secret-like content]" in rendered_json
    assert "Bearer abcdefghijklmnopqrstuvwxyz" not in rendered_json


def test_render_markdown_includes_operator_fields(tmp_path):
    db = _seed_db(tmp_path / "state.db")
    try:
        report = TrajectoryRadar(db).generate(days=1, limit=1)
    finally:
        db.close()

    markdown = render_markdown(report)
    assert "## Action Candidates" in markdown
    assert "**Why now:**" in markdown
    assert "**First 30-minute move:**" in markdown
    assert "**Proof it worked:**" in markdown


def test_cron_silent_contract_drift_is_classified(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    now = time.time()
    try:
        db.create_session("s-cron", "cron", model="qwen3-coder-30b", cwd="/tmp/hermes")
        db.append_message("s-cron", "assistant", "[SILENT] but also a noisy payload", timestamp=now - 5)
        report = TrajectoryRadar(db).generate(days=1, limit=10)
    finally:
        db.close()

    cron = next(candidate for candidate in report["candidates"] if candidate["id"] == "quiet-learning-cron-digest")
    assert cron["route"] == "CRON"
    assert cron["evidence_refs"][0]["signal"] == "cron_silent_contract_drift"


def test_trajectory_cli_writes_json_report(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    db = _seed_db(home / "state.db")
    db.close()
    out = tmp_path / "radar.json"

    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "hermes_cli.main",
            "trajectory",
            "radar",
            "--days",
            "1",
            "--format",
            "json",
            "--out",
            str(out),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "Trajectory radar written to:" in result.stdout
    payload = json.loads(out.read_text())
    assert payload["candidates"]
    assert payload["privacy"]["raw_transcripts_included"] is False
