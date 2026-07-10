from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from agent.trajectory_radar import (
    CandidateStore,
    TrajectoryRadar,
    render_markdown,
)
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


# ---------------------------------------------------------------------------
# Salvaged radar tests
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Candidate lifecycle tests
# ---------------------------------------------------------------------------


def test_candidate_store_sync_and_list(tmp_path):
    store_path = tmp_path / "radar_candidates.json"
    store = CandidateStore(path=store_path)

    db = _seed_db(tmp_path / "state.db")
    try:
        report = TrajectoryRadar(db).generate(days=1, limit=10)
    finally:
        db.close()

    regressed = store.sync_from_report(report)
    # First sync — all candidates are new, nothing to regress.
    assert regressed == []

    records = store.list()
    fps = {r.fingerprint for r in records}
    assert "done-means-proven-gatekeeper" in fps
    assert "local-first-dispatch-firewall" in fps
    assert store_path.exists()

    # Reload from disk to verify persistence.
    store2 = CandidateStore(path=store_path)
    assert {r.fingerprint for r in store2.list()} == fps


def test_candidate_transition_accept_resolve_ignore(tmp_path):
    store_path = tmp_path / "radar_candidates.json"
    store = CandidateStore(path=store_path)

    db = _seed_db(tmp_path / "state.db")
    try:
        report = TrajectoryRadar(db).generate(days=1, limit=10)
    finally:
        db.close()

    store.sync_from_report(report)
    fp = "done-means-proven-gatekeeper"

    assert store.get(fp).status == "new"

    rec = store.transition(fp, "accepted", note="working on it")
    assert rec.status == "accepted"
    assert rec.note == "working on it"
    assert rec.last_action_at >= rec.first_seen

    rec = store.transition(fp, "resolved")
    assert rec.status == "resolved"
    assert rec.resolved_at is not None

    # Reload to verify persistence of resolved state.
    store2 = CandidateStore(path=store_path)
    assert store2.get(fp).status == "resolved"
    assert store2.get(fp).resolved_at is not None


def test_candidate_regression_on_resolved_with_fresh_evidence(tmp_path):
    """A resolved candidate regresses only when a new evidence ref appears."""
    store_path = tmp_path / "radar_candidates.json"
    store = CandidateStore(path=store_path)

    db = _seed_db(tmp_path / "state.db")
    try:
        report = TrajectoryRadar(db).generate(days=1, limit=10)
    finally:
        db.close()

    store.sync_from_report(report)
    fp = "done-means-proven-gatekeeper"
    store.transition(fp, "resolved")

    # An unchanged rerun is not new evidence and leaves confirmation pending.
    db2 = SessionDB(db_path=tmp_path / "state.db")
    try:
        report2 = TrajectoryRadar(db2).generate(days=1, limit=10)
    finally:
        db2.close()

    assert store.sync_from_report(report2) == []
    assert store.get(fp).status == "resolved"
    assert store.get(fp).confirmation == "pending"

    # A new matching message is fresh evidence and must regress the item.
    db3 = SessionDB(db_path=tmp_path / "state.db")
    db3.append_message("s-verify", "user", "Did you check the new failure?", timestamp=time.time())
    try:
        report3 = TrajectoryRadar(db3).generate(days=1, limit=10)
    finally:
        db3.close()

    regressed = store.sync_from_report(report3)
    assert fp in regressed
    assert store.get(fp).status == "regressed"
    assert store.get(fp).resolved_at is None


def test_candidate_regression_on_accepted_with_growing_evidence(tmp_path):
    """An accepted candidate whose evidence count grows regresses."""
    store_path = tmp_path / "radar_candidates.json"
    store = CandidateStore(path=store_path)

    db = _seed_db(tmp_path / "state.db")
    try:
        report = TrajectoryRadar(db).generate(days=1, limit=10)
    finally:
        db.close()

    store.sync_from_report(report)
    fp = "done-means-proven-gatekeeper"
    initial_count = store.get(fp).last_evidence_count
    assert initial_count > 0

    store.transition(fp, "accepted")

    # Add more messages that trigger the same signal → evidence grows.
    db2 = SessionDB(db_path=tmp_path / "state.db")
    now = time.time()
    db2.append_message("s-verify", "user", "Did you check again? Are we done now?", timestamp=now - 1)
    try:
        report2 = TrajectoryRadar(db2).generate(days=1, limit=10)
    finally:
        db2.close()

    our_cand = next(c for c in report2["candidates"] if c["id"] == fp)
    fresh_count = our_cand["evidence_count"]
    assert fresh_count > initial_count, f"expected growth: {fresh_count} > {initial_count}"

    regressed = store.sync_from_report(report2)
    assert fp in regressed
    assert store.get(fp).status == "regressed"


def test_candidate_regression_tracks_newest_evidence_after_ref_cap(tmp_path):
    """Fresh evidence remains visible after a candidate exceeds the 20-ref cap."""
    store = CandidateStore(path=tmp_path / "radar_candidates.json")
    db_path = tmp_path / "state.db"

    db = _seed_db(db_path)
    now = time.time()
    for index in range(25):
        db.append_message(
            "s-verify",
            "user",
            f"Did you check capped evidence {index}?",
            timestamp=now + index,
        )
    try:
        report = TrajectoryRadar(db).generate(days=1, limit=10)
    finally:
        db.close()

    candidate = next(
        item
        for item in report["candidates"]
        if item["id"] == "done-means-proven-gatekeeper"
    )
    assert candidate["evidence_count"] > 20
    assert len(candidate["evidence_refs"]) == 20

    store.sync_from_report(report)
    store.transition(candidate["id"], "resolved")

    db = SessionDB(db_path=db_path)
    db.append_message(
        "s-verify",
        "user",
        "Did you check the evidence after the cap?",
        timestamp=time.time() + 100,
    )
    try:
        refreshed = TrajectoryRadar(db).generate(days=1, limit=10)
    finally:
        db.close()

    assert store.sync_from_report(refreshed) == [candidate["id"]]
    assert store.get(candidate["id"]).status == "regressed"


def test_candidate_ignored_not_auto_regressed(tmp_path):
    """An ignored candidate is never automatically regressed."""
    store_path = tmp_path / "radar_candidates.json"
    store = CandidateStore(path=store_path)

    db = _seed_db(tmp_path / "state.db")
    try:
        report = TrajectoryRadar(db).generate(days=1, limit=10)
    finally:
        db.close()

    store.sync_from_report(report)
    fp = "done-means-proven-gatekeeper"
    store.transition(fp, "ignored")

    # Fresh radar run.
    db2 = _seed_db(tmp_path / "state.db")
    try:
        report2 = TrajectoryRadar(db2).generate(days=1, limit=10)
    finally:
        db2.close()

    regressed = store.sync_from_report(report2)
    assert fp not in regressed
    assert store.get(fp).status == "ignored"


def test_candidate_show_unknown_fingerprint(tmp_path):
    store_path = tmp_path / "radar_candidates.json"
    store = CandidateStore(path=store_path)
    assert store.get("nonexistent-fingerprint") is None


def test_candidate_list_status_filter(tmp_path):
    store_path = tmp_path / "radar_candidates.json"
    store = CandidateStore(path=store_path)

    db = _seed_db(tmp_path / "state.db")
    try:
        report = TrajectoryRadar(db).generate(days=1, limit=10)
    finally:
        db.close()

    store.sync_from_report(report)
    fp = "done-means-proven-gatekeeper"
    store.transition(fp, "accepted")

    accepted = store.list(status="accepted")
    assert len(accepted) == 1
    assert accepted[0].fingerprint == fp

    new_records = store.list(status="new")
    assert fp not in {r.fingerprint for r in new_records}


# ---------------------------------------------------------------------------
# CLI E2E tests for candidate lifecycle
# ---------------------------------------------------------------------------


def _run_cli(home: Path, extra_args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(cwd)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "trajectory", *extra_args],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )


def test_cli_radar_then_candidates_lifecycle(tmp_path):
    """Full E2E: radar syncs store → list → accept → resolve → list."""
    home = tmp_path / "home"
    home.mkdir()
    cwd = Path(__file__).resolve().parents[2]

    db = _seed_db(home / "state.db")
    db.close()

    # 1. Run radar (syncs into store by default).
    result = _run_cli(home, ["radar", "--days", "1", "--format", "json", "--out", str(tmp_path / "radar.json")], cwd)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "Trajectory radar written to:" in result.stdout

    # 2. List candidates.
    result = _run_cli(home, ["candidates", "list", "--json"], cwd)
    assert result.returncode == 0, result.stderr + result.stdout
    records = json.loads(result.stdout)
    fps = {r["fingerprint"] for r in records}
    assert "done-means-proven-gatekeeper" in fps
    statuses_before = {r["fingerprint"]: r["status"] for r in records}
    fp = "done-means-proven-gatekeeper"
    assert statuses_before[fp] == "new"

    # 3. Accept a candidate.
    result = _run_cli(home, ["candidates", "accept", fp], cwd)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "→ accepted" in result.stdout

    # 4. Resolve it.
    result = _run_cli(home, ["candidates", "resolve", fp], cwd)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "→ resolved" in result.stdout

    # 5. Show it — verify resolved status.
    result = _run_cli(home, ["candidates", "show", fp], cwd)
    assert result.returncode == 0, result.stderr + result.stdout
    rec = json.loads(result.stdout)
    assert rec["status"] == "resolved"
    assert rec["resolved_at"] is not None

    # 6. The candidates file exists in the hermes home (not in state.db).
    candidates_file = home / "radar_candidates.json"
    assert candidates_file.exists()


def test_cli_radar_regression_then_defer_ignore(tmp_path):
    """E2E: radar regression on re-run, plus defer and ignore commands."""
    home = tmp_path / "home"
    home.mkdir()
    cwd = Path(__file__).resolve().parents[2]

    db = _seed_db(home / "state.db")
    db.close()

    # 1. Radar → sync.
    _run_cli(home, ["radar", "--days", "1", "--format", "json", "--out", str(tmp_path / "r1.json")], cwd)

    fp = "done-means-proven-gatekeeper"

    # 2. Resolve it via CLI.
    _run_cli(home, ["candidates", "resolve", fp], cwd)

    # 3. Add a fresh matching signal, then rerun radar → regression.
    db = SessionDB(db_path=home / "state.db")
    db.append_message("s-verify", "user", "Did you verify the new failure?", timestamp=time.time())
    db.close()
    result = _run_cli(home, ["radar", "--days", "1", "--format", "json", "--out", str(tmp_path / "r2.json")], cwd)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "regressed" in result.stdout

    # 4. Verify status is regressed.
    result = _run_cli(home, ["candidates", "show", fp], cwd)
    assert result.returncode == 0, result.stderr + result.stdout
    rec = json.loads(result.stdout)
    assert rec["status"] == "regressed"

    # 5. Defer a different candidate.
    fp2 = "local-first-dispatch-firewall"
    result = _run_cli(home, ["candidates", "defer", fp2, "--note", "later"], cwd)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "→ deferred" in result.stdout

    # 6. Ignore it.
    result = _run_cli(home, ["candidates", "ignore", fp2], cwd)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "→ ignored" in result.stdout


def test_cli_unknown_candidate_exits_nonzero(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    cwd = Path(__file__).resolve().parents[2]

    result = _run_cli(home, ["candidates", "show", "missing"], cwd)

    assert result.returncode != 0
    assert "No candidate found" in result.stderr


def test_cli_radar_database_failure_exits_nonzero(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "state.db").mkdir()
    cwd = Path(__file__).resolve().parents[2]

    result = _run_cli(home, ["radar", "--days", "1"], cwd)

    assert result.returncode != 0
    assert "Error generating trajectory radar" in result.stderr
