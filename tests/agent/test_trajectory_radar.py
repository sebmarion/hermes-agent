from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent.trajectory_radar import (
    CandidateStore,
    CandidateStoreError,
    TrajectoryRadar,
    render_markdown,
)
from hermes_state import SessionDB


def _candidate_report(
    *candidate_ids: str,
    evidence_suffix: str = "initial",
    evidence_observed_at: float | None = None,
    complete: bool = True,
    source: str | None = None,
    scan_time: float | None = None,
    window_from: float | None = None,
) -> dict:
    raw_scanned_at = time.time() if scan_time is None else scan_time
    generated = datetime.fromtimestamp(raw_scanned_at, tz=timezone.utc)
    scanned_at = generated.timestamp()
    observed_from = scanned_at - 86400 if window_from is None else window_from
    return {
        "generated_at": generated.isoformat(),
        "window": {
            "days": (scanned_at - observed_from) / 86400,
            "from_epoch": observed_from,
            "to_epoch": scanned_at,
        },
        "candidate_set_complete": complete,
        "candidate_count_before_limit": (
            len(candidate_ids) if complete else len(candidate_ids) + 1
        ),
        "source_filter": source,
        "candidates": [
            {
                "id": candidate_id,
                "title": f"Candidate {candidate_id}",
                "route": "FIX",
                "score": 42.0,
                "evidence_count": 1,
                "evidence_refs": [
                    {
                        "session_id": f"private-session-{candidate_id}",
                        "message_id": 7,
                        "signal": f"signal-{evidence_suffix}",
                        "observed_at": (
                            scanned_at
                            if evidence_observed_at is None
                            else evidence_observed_at
                        ),
                        "snippet": "seb@example.com /Users/seb/private",
                    }
                ],
            }
            for candidate_id in candidate_ids
        ],
    }


def _candidate_store_process_writer(path: str, fingerprint: str, status: str, ready, start) -> None:
    store = CandidateStore(path=path)
    ready.put(fingerprint)
    if not start.wait(timeout=10):
        raise RuntimeError("candidate-store process start timed out")
    store.transition(fingerprint, status)


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


def test_radar_reports_whether_candidate_set_is_complete(tmp_path):
    db = _seed_db(tmp_path / "state.db")
    try:
        complete = TrajectoryRadar(db).generate(days=1, limit=0)
        truncated = TrajectoryRadar(db).generate(days=1, limit=1)
    finally:
        db.close()

    assert complete["candidate_set_complete"] is True
    assert complete["candidate_count_before_limit"] == len(complete["candidates"])
    assert truncated["candidate_set_complete"] is False
    assert truncated["candidate_count_before_limit"] > len(truncated["candidates"])


def test_radar_generated_timestamp_never_precedes_window_end(
    tmp_path, monkeypatch
):
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(
        "agent.trajectory_radar.time.time",
        lambda: 1786126460.9114702,
    )
    try:
        report = TrajectoryRadar(db).generate(days=1, limit=0)
    finally:
        db.close()

    generated_at = datetime.fromisoformat(report["generated_at"]).timestamp()
    assert generated_at >= report["window"]["to_epoch"]


def test_radar_window_bounds_evidence_by_message_timestamp(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    now = time.time()
    try:
        db.create_session("old-session", "cli", model="qwen")
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (now - (2 * 86400), "old-session"),
        )
        db._conn.commit()
        db.append_message(
            "old-session",
            "user",
            "Did you check the recent regression?",
            timestamp=now - 10,
        )

        db.create_session("future-session", "cli", model="qwen")
        db.append_message(
            "future-session",
            "user",
            "Did you check the future regression?",
            timestamp=now + 3600,
        )

        report = TrajectoryRadar(db).generate(days=1, limit=0)
    finally:
        db.close()

    candidate = next(
        item
        for item in report["candidates"]
        if item["id"] == "done-means-proven-gatekeeper"
    )
    assert {ref["session_id"] for ref in candidate["evidence_refs"]} == {
        "old-session"
    }


def test_routing_candidate_hosted_cap_keeps_newest_session(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    now = time.time()
    try:
        for index in range(51):
            session_id = f"hosted-{index:03d}"
            db.create_session(session_id, "cli", model="gpt-test")
            db.update_token_counts(
                session_id,
                input_tokens=1,
                output_tokens=1,
                billing_provider="openai-codex",
            )
            db._conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?",
                (now - (51 - index), session_id),
            )
        db._conn.commit()
        report = TrajectoryRadar(db).generate(days=1, limit=0)
    finally:
        db.close()

    candidate = next(
        item
        for item in report["candidates"]
        if item["id"] == "local-first-dispatch-firewall"
    )
    assert "hosted-050" in {
        ref["session_id"] for ref in candidate["evidence_refs"]
    }


@pytest.mark.parametrize("days", [0, -1])
def test_radar_rejects_nonpositive_observation_windows(tmp_path, days):
    db = _seed_db(tmp_path / "state.db")
    try:
        with pytest.raises(ValueError, match="days must be positive"):
            TrajectoryRadar(db).generate(days=days, limit=0)
    finally:
        db.close()


def test_candidate_store_is_profile_aware_and_persists_hashed_evidence_only(
    tmp_path, monkeypatch
):
    home = tmp_path / "profile-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    store = CandidateStore()

    assert store.path == home / "radar_candidates.json"
    assert store.sync_from_report(_candidate_report("candidate-a")) == []

    record = store.get("candidate-a")
    assert record is not None
    assert record.fingerprint == "candidate-a"
    assert record.status == "new"
    assert len(record.evidence_hashes) == 1
    assert len(record.evidence_hashes[0]) == 64
    int(record.evidence_hashes[0], 16)

    persisted = store.path.read_text(encoding="utf-8")
    assert "private-session" not in persisted
    assert "seb@example.com" not in persisted
    assert "/Users/seb" not in persisted
    assert "message_id" not in persisted
    assert "snippet" not in persisted


def test_candidate_store_sync_is_byte_idempotent(tmp_path):
    store = CandidateStore(path=tmp_path / "radar_candidates.json")
    report = _candidate_report("candidate-a")

    assert store.sync_from_report(report) == []
    before = store.path.read_bytes()
    assert store.sync_from_report(report) == []

    assert store.path.read_bytes() == before


@pytest.mark.parametrize(
    ("command_status", "stored_status"),
    [
        ("accepted", "accepted"),
        ("deferred", "deferred"),
        ("resolved", "resolved"),
        ("ignored", "ignored"),
        ("regressed", "regressed"),
    ],
)
def test_candidate_store_supports_all_lifecycle_statuses(
    tmp_path, command_status, stored_status
):
    store = CandidateStore(path=tmp_path / "radar_candidates.json")
    store.sync_from_report(_candidate_report("candidate-a"))

    record = store.transition("candidate-a", command_status)

    assert record.status == stored_status
    assert CandidateStore(path=store.path).get("candidate-a").status == stored_status


def test_candidate_store_regresses_only_for_fresh_evidence(tmp_path):
    store = CandidateStore(path=tmp_path / "radar_candidates.json")
    initial = _candidate_report("candidate-a")
    store.sync_from_report(initial)
    store.transition("candidate-a", "resolved")

    assert store.sync_from_report(initial) == []
    assert store.get("candidate-a").status == "resolved"
    assert store.get("candidate-a").confirmation == "pending"

    fresh = _candidate_report("candidate-a", evidence_suffix="fresh")
    assert store.sync_from_report(fresh) == ["candidate-a"]
    assert store.get("candidate-a").status == "regressed"
    assert store.get("candidate-a").resolved_at is None


def test_candidate_store_does_not_regress_for_unseen_pre_action_evidence(tmp_path):
    store = CandidateStore(path=tmp_path / "radar_candidates.json")
    initial = _candidate_report("candidate-a")
    store.sync_from_report(initial)
    resolved = store.transition("candidate-a", "resolved")

    later_scan = time.time()
    historical = _candidate_report(
        "candidate-a",
        evidence_suffix="historical",
        evidence_observed_at=(resolved.last_action_at or 0.0) - 100,
        scan_time=later_scan,
    )

    assert store.sync_from_report(historical) == []
    assert store.get("candidate-a").status == "resolved"


def test_candidate_store_rejects_report_generated_before_action(
    tmp_path,
):
    store = CandidateStore(path=tmp_path / "radar_candidates.json")
    store.sync_from_report(_candidate_report("candidate-a"))
    resolved = store.transition("candidate-a", "resolved")
    action_at = resolved.last_action_at

    pre_action_report = _candidate_report(
        "candidate-a",
        evidence_suffix="post-action-from-pre-action-report",
        evidence_observed_at=action_at + 0.1,
        scan_time=action_at + 0.25,
    )
    pre_action_report["generated_at"] = datetime.fromtimestamp(
        action_at - 0.25,
        tz=timezone.utc,
    ).isoformat()

    with pytest.raises(CandidateStoreError, match="timestamp/window"):
        store.sync_from_report(pre_action_report)
    assert store.get("candidate-a").status == "resolved"


def test_candidate_ref_cap_keeps_newest_post_action_evidence(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    now = time.time()
    try:
        db.create_session("capped-session", "cli", model="qwen")
        for index in range(20):
            db.append_message(
                "capped-session",
                "assistant",
                "Done.",
                timestamp=now - (20 - index),
            )
        initial = TrajectoryRadar(db).generate(days=1, limit=0)

        store = CandidateStore(path=tmp_path / "radar_candidates.json")
        store.sync_from_report(initial)
        store.transition("done-means-proven-gatekeeper", "resolved")

        db.append_message(
            "capped-session",
            "user",
            "Did you check the newest evidence?",
            timestamp=time.time(),
        )
        refreshed = TrajectoryRadar(db).generate(days=1, limit=0)
    finally:
        db.close()

    assert store.sync_from_report(refreshed) == [
        "done-means-proven-gatekeeper"
    ]
    assert store.get("done-means-proven-gatekeeper").status == "regressed"


def test_candidate_store_tracks_fresh_evidence_after_report_ref_cap(tmp_path):
    store = CandidateStore(path=tmp_path / "radar_candidates.json")
    db_path = tmp_path / "state.db"
    db = _seed_db(db_path)
    now = time.time()
    for index in range(25):
        db.append_message(
            "s-verify",
            "user",
            f"Did you check capped evidence {index}?",
            timestamp=now - (25 - index),
        )
    try:
        report = TrajectoryRadar(db).generate(days=1, limit=0)
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
        "Did you check evidence after the cap?",
        timestamp=time.time(),
    )
    try:
        refreshed = TrajectoryRadar(db).generate(days=1, limit=0)
    finally:
        db.close()

    assert store.sync_from_report(refreshed) == [candidate["id"]]
    assert store.get(candidate["id"]).status == "regressed"


def test_candidate_store_never_auto_regresses_ignored_candidate(tmp_path):
    store = CandidateStore(path=tmp_path / "radar_candidates.json")
    store.sync_from_report(_candidate_report("candidate-a"))
    store.transition("candidate-a", "ignored")

    assert store.sync_from_report(
        _candidate_report("candidate-a", evidence_suffix="fresh")
    ) == []
    assert store.get("candidate-a").status == "ignored"


def test_resolution_absence_confirmation_requires_complete_unfiltered_report(tmp_path):
    store = CandidateStore(path=tmp_path / "radar_candidates.json")
    store.sync_from_report(_candidate_report("candidate-a"))
    store.transition("candidate-a", "resolved")

    store.sync_from_report(_candidate_report(complete=False))
    assert store.get("candidate-a").confirmation == "pending"

    store.sync_from_report(_candidate_report(complete=True, source="cli"))
    assert store.get("candidate-a").confirmation == "pending"

    resolved_at = store.get("candidate-a").resolved_at
    store.sync_from_report(
        _candidate_report(complete=True, scan_time=resolved_at - 1)
    )
    assert store.get("candidate-a").confirmation == "pending"

    missing_scope = _candidate_report(complete=True)
    missing_scope.pop("source_filter")
    with pytest.raises(CandidateStoreError, match="source_filter"):
        store.sync_from_report(missing_scope)
    assert store.get("candidate-a").confirmation == "pending"

    post_action_scan = time.time()
    store.sync_from_report(
        _candidate_report(
            complete=True,
            scan_time=post_action_scan,
            window_from=(resolved_at + post_action_scan) / 2,
        )
    )
    assert store.get("candidate-a").confirmation == "pending"

    forged_post_action_window = _candidate_report(
        complete=True,
        scan_time=resolved_at + 1,
        window_from=resolved_at - 10,
    )
    forged_post_action_window["generated_at"] = datetime.fromtimestamp(
        resolved_at - 1, tz=timezone.utc
    ).isoformat()
    with pytest.raises(CandidateStoreError, match="timestamp/window"):
        store.sync_from_report(forged_post_action_window)
    assert store.get("candidate-a").confirmation == "pending"

    store.sync_from_report(_candidate_report(complete=True))
    assert store.get("candidate-a").confirmation == "confirmed"

    store.sync_from_report(
        _candidate_report("candidate-a", complete=True, scan_time=resolved_at - 1)
    )
    assert store.get("candidate-a").confirmation == "confirmed"


def test_candidate_store_rejects_malformed_report_envelope_without_writing(tmp_path):
    store = CandidateStore(path=tmp_path / "radar_candidates.json")
    store.sync_from_report(_candidate_report("candidate-a"))
    before = store.path.read_bytes()

    malformed = _candidate_report("candidate-a")
    malformed.pop("candidates")
    with pytest.raises(CandidateStoreError, match="candidates"):
        store.sync_from_report(malformed)

    assert store.path.read_bytes() == before

    nonfinite = _candidate_report("candidate-a")
    nonfinite["window"]["days"] = float("nan")
    with pytest.raises(CandidateStoreError, match="window days"):
        store.sync_from_report(nonfinite)

    assert store.path.read_bytes() == before


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (
            lambda report: report["window"].__setitem__("days", 2),
            "window span",
        ),
        (
            lambda report: report.__setitem__(
                "generated_at",
                datetime.fromtimestamp(
                    report["window"]["to_epoch"] - 60,
                    tz=timezone.utc,
                ).isoformat(),
            ),
            "timestamp/window",
        ),
    ],
)
def test_candidate_store_rejects_internally_inconsistent_report_time(
    tmp_path, mutation, error
):
    store = CandidateStore(path=tmp_path / "radar_candidates.json")
    report = _candidate_report("candidate-a")
    mutation(report)

    with pytest.raises(CandidateStoreError, match=error):
        store.sync_from_report(report)

    assert not store.path.exists()


def test_candidate_store_rejects_evidence_after_report_generation(tmp_path):
    store = CandidateStore(path=tmp_path / "radar_candidates.json")
    store.sync_from_report(_candidate_report("candidate-a"))
    store.transition("candidate-a", "resolved")
    generated_at = time.time()
    future_evidence = _candidate_report(
        "candidate-a",
        evidence_suffix="future-evidence",
        evidence_observed_at=generated_at + 0.75,
        scan_time=generated_at + 0.75,
    )
    future_evidence["generated_at"] = datetime.fromtimestamp(
        generated_at,
        tz=timezone.utc,
    ).isoformat()

    with pytest.raises(CandidateStoreError, match="timestamp/window"):
        store.sync_from_report(future_evidence)

    assert store.get("candidate-a").status == "resolved"


def test_candidate_store_rejects_future_empty_window_for_confirmation(tmp_path):
    store = CandidateStore(path=tmp_path / "radar_candidates.json")
    store.sync_from_report(_candidate_report("candidate-a"))
    resolved = store.transition("candidate-a", "resolved")
    generated_at = time.time()
    future_empty = _candidate_report(
        complete=True,
        scan_time=generated_at + 0.75,
        window_from=resolved.last_action_at - 1,
    )
    future_empty["generated_at"] = datetime.fromtimestamp(
        generated_at,
        tz=timezone.utc,
    ).isoformat()

    with pytest.raises(CandidateStoreError, match="timestamp/window"):
        store.sync_from_report(future_empty)

    assert store.get("candidate-a").confirmation == "pending"


def test_candidate_store_accepts_generation_after_window_within_tolerance(tmp_path):
    store = CandidateStore(path=tmp_path / "radar_candidates.json")
    generated_at = time.time()
    report = _candidate_report(
        "candidate-a",
        scan_time=generated_at - 0.75,
    )
    report["generated_at"] = datetime.fromtimestamp(
        generated_at,
        tz=timezone.utc,
    ).isoformat()

    assert store.sync_from_report(report) == []
    assert store.get("candidate-a") is not None


@pytest.mark.parametrize(
    ("scan_offset", "error"),
    [(-600, "stale"), (5, "future")],
)
def test_candidate_store_rejects_stale_or_future_report(
    tmp_path, scan_offset, error
):
    store = CandidateStore(path=tmp_path / "radar_candidates.json")
    report = _candidate_report(
        "candidate-a",
        scan_time=time.time() + scan_offset,
    )

    with pytest.raises(CandidateStoreError, match=error):
        store.sync_from_report(report)

    assert not store.path.exists()


def test_candidate_store_fails_closed_on_corrupt_or_ambiguous_state(tmp_path):
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    before = corrupt.read_bytes()
    with pytest.raises(CandidateStoreError, match="corrupt"):
        CandidateStore(path=corrupt)
    assert corrupt.read_bytes() == before

    ambiguous = tmp_path / "ambiguous.json"
    record = {
        "fingerprint": "duplicate",
        "title": "Duplicate",
        "route": "FIX",
        "status": "new",
        "first_seen": 1.0,
        "last_seen": 1.0,
        "resolved_at": None,
        "last_action_at": 1.0,
        "last_evidence_count": 0,
        "last_score": 0.0,
        "confirmation": "unconfirmed",
        "evidence_hashes": [],
    }
    ambiguous.write_text(
        json.dumps({"version": 1, "records": [record, record]}), encoding="utf-8"
    )
    with pytest.raises(CandidateStoreError, match="duplicate"):
        CandidateStore(path=ambiguous)


def test_candidate_store_failed_atomic_write_preserves_authoritative_state(
    tmp_path, monkeypatch
):
    import agent.trajectory_radar as radar_module

    store = CandidateStore(path=tmp_path / "radar_candidates.json")
    store.sync_from_report(_candidate_report("candidate-a"))
    before = store.path.read_bytes()

    def fail_write(*_args, **_kwargs):
        raise OSError("simulated durable write failure")

    monkeypatch.setattr(radar_module, "atomic_json_write", fail_write)
    with pytest.raises(CandidateStoreError, match="write"):
        store.transition("candidate-a", "accepted")

    assert store.path.read_bytes() == before
    assert CandidateStore(path=store.path).get("candidate-a").status == "new"


def test_candidate_store_serializes_preloaded_thread_writers(tmp_path):
    path = tmp_path / "radar_candidates.json"
    CandidateStore(path=path).sync_from_report(
        _candidate_report("candidate-a", "candidate-b")
    )
    stores = [CandidateStore(path=path), CandidateStore(path=path)]
    barrier = threading.Barrier(3)
    errors: list[BaseException] = []

    def write(index: int, fingerprint: str, status: str) -> None:
        try:
            barrier.wait(timeout=5)
            stores[index].transition(fingerprint, status)
        except BaseException as exc:  # surfaced in the parent test thread
            errors.append(exc)

    threads = [
        threading.Thread(target=write, args=(0, "candidate-a", "accepted")),
        threading.Thread(target=write, args=(1, "candidate-b", "deferred")),
    ]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert not any(thread.is_alive() for thread in threads)
    records = {record.fingerprint: record for record in CandidateStore(path=path).list()}
    assert records["candidate-a"].status == "accepted"
    assert records["candidate-b"].status == "deferred"


def test_candidate_store_serializes_preloaded_process_writers(tmp_path):
    path = tmp_path / "radar_candidates.json"
    CandidateStore(path=path).sync_from_report(
        _candidate_report("candidate-a", "candidate-b")
    )
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Queue()
    start = ctx.Event()
    processes = [
        ctx.Process(
            target=_candidate_store_process_writer,
            args=(str(path), "candidate-a", "accepted", ready, start),
        ),
        ctx.Process(
            target=_candidate_store_process_writer,
            args=(str(path), "candidate-b", "deferred", ready, start),
        ),
    ]
    for process in processes:
        process.start()
    assert {ready.get(timeout=10), ready.get(timeout=10)} == {
        "candidate-a",
        "candidate-b",
    }
    start.set()
    for process in processes:
        process.join(timeout=15)

    assert [process.exitcode for process in processes] == [0, 0]
    records = {record.fingerprint: record for record in CandidateStore(path=path).list()}
    assert records["candidate-a"].status == "accepted"
    assert records["candidate-b"].status == "deferred"
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1
    assert not list(tmp_path.glob(".*.tmp"))


def _run_trajectory_cli(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(root)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "trajectory", *args],
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )


def test_trajectory_cli_sync_and_full_candidate_lifecycle(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    db = _seed_db(home / "state.db")
    db.close()

    radar = _run_trajectory_cli(
        home, "radar", "--days", "1", "--limit", "0", "--format", "json"
    )
    assert radar.returncode == 0, radar.stderr + radar.stdout
    assert json.loads(radar.stdout)["candidates"]

    listed = _run_trajectory_cli(home, "candidates", "list", "--json")
    assert listed.returncode == 0, listed.stderr + listed.stdout
    records = json.loads(listed.stdout)
    fingerprint = next(
        record["fingerprint"]
        for record in records
        if record["fingerprint"] == "done-means-proven-gatekeeper"
    )

    shown = _run_trajectory_cli(home, "candidates", "show", fingerprint)
    assert shown.returncode == 0, shown.stderr + shown.stdout
    assert json.loads(shown.stdout)["status"] == "new"

    for command, status in (
        ("accept", "accepted"),
        ("defer", "deferred"),
        ("resolve", "resolved"),
        ("ignore", "ignored"),
    ):
        changed = _run_trajectory_cli(home, "candidates", command, fingerprint)
        assert changed.returncode == 0, changed.stderr + changed.stdout
        assert status in changed.stdout
        assert json.loads(
            _run_trajectory_cli(home, "candidates", "show", fingerprint).stdout
        )["status"] == status

    hidden = json.loads(
        _run_trajectory_cli(home, "candidates", "list", "--json").stdout
    )
    assert fingerprint not in {record["fingerprint"] for record in hidden}
    all_records = json.loads(
        _run_trajectory_cli(
            home, "candidates", "list", "--all", "--json"
        ).stdout
    )
    assert fingerprint in {record["fingerprint"] for record in all_records}


def test_trajectory_cli_explicit_sync_and_no_sync_flags(tmp_path):
    no_sync_home = tmp_path / "no-sync-home"
    no_sync_home.mkdir()
    _seed_db(no_sync_home / "state.db").close()

    no_sync = _run_trajectory_cli(
        no_sync_home, "radar", "--days", "1", "--no-sync", "--format", "json"
    )
    assert no_sync.returncode == 0, no_sync.stderr + no_sync.stdout
    assert not (no_sync_home / "radar_candidates.json").exists()

    sync_home = tmp_path / "sync-home"
    sync_home.mkdir()
    _seed_db(sync_home / "state.db").close()
    sync = _run_trajectory_cli(
        sync_home, "radar", "--days", "1", "--sync", "--format", "json"
    )
    assert sync.returncode == 0, sync.stderr + sync.stdout
    assert (sync_home / "radar_candidates.json").exists()


def test_trajectory_cli_unknown_candidate_and_database_failures_are_nonzero(
    tmp_path,
):
    home = tmp_path / "home"
    home.mkdir()
    SessionDB(db_path=home / "state.db").close()

    unknown = _run_trajectory_cli(home, "candidates", "show", "missing")
    assert unknown.returncode != 0
    assert "No candidate found" in unknown.stderr

    broken_home = tmp_path / "broken-home"
    broken_home.mkdir()
    (broken_home / "state.db").mkdir()
    broken = _run_trajectory_cli(broken_home, "radar", "--days", "1")
    assert broken.returncode != 0
    assert "Error generating trajectory radar" in broken.stderr

    invalid_window = _run_trajectory_cli(home, "radar", "--days", "-1")
    assert invalid_window.returncode != 0
    assert "days must be positive" in invalid_window.stderr
