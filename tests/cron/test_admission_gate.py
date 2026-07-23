"""Cross-process cron admission fence contracts.

The drain proof is only sound when every process admits cron work through the
same profile-scoped lock and the gate epoch plus active leases are observed in
one atomic snapshot.
"""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
import threading

import pytest


def _claim_from_child(home: str, job_id: str, result_queue) -> None:
    os.environ["HERMES_HOME"] = home
    from cron.admission import claim_cron_admission

    lease = claim_cron_admission(job_id, source="child")
    result_queue.put(lease is not None)


def _reopen_from_child(home: str, result_queue) -> None:
    os.environ["HERMES_HOME"] = home
    from cron.admission import set_cron_admission_paused

    result_queue.put(
        set_cron_admission_paused(False, reason="non-owner-release")
    )


def test_gate_close_snapshots_active_lease_and_rejects_other_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from cron.admission import (
        claim_cron_admission,
        cron_admission_snapshot,
        release_cron_admission,
        set_cron_admission_paused,
    )

    lease = claim_cron_admission("running-job", source="builtin_due")
    assert lease is not None

    closed = set_cron_admission_paused(True, reason="test-drain")
    assert closed["verified"] is True
    assert closed["accepting"] is False
    assert closed["active_count"] == 1
    assert closed["active_job_ids"] == ["running-job"]
    assert closed["gate_epoch"] == lease.gate_epoch + 1

    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue()
    child = ctx.Process(
        target=_claim_from_child,
        args=(str(tmp_path), "late-job", result_queue),
    )
    child.start()
    child.join(timeout=10)
    assert child.exitcode == 0
    assert result_queue.get(timeout=2) is False

    release_cron_admission(lease)
    zero = cron_admission_snapshot()
    assert zero["gate_epoch"] == closed["gate_epoch"]
    assert zero["active_count"] == 0
    assert zero["active_job_ids"] == []


def test_live_non_owner_process_cannot_reopen_manual_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from cron.admission import set_cron_admission_paused

    closed = set_cron_admission_paused(True, reason="owner-drain")
    assert closed["accepting"] is False

    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue()
    child = ctx.Process(
        target=_reopen_from_child,
        args=(str(tmp_path), result_queue),
    )
    child.start()
    child.join(timeout=10)
    assert child.exitcode == 0
    refused = result_queue.get(timeout=2)
    assert refused["verified"] is True
    assert refused["accepting"] is False
    assert refused["gate_epoch"] == closed["gate_epoch"]

    opened = set_cron_admission_paused(False, reason="owner-release")
    assert opened["accepting"] is True


def test_dead_process_lease_is_pruned_before_snapshot_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from cron.admission import cron_admission_snapshot

    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue()
    child = ctx.Process(
        target=_claim_from_child,
        args=(str(tmp_path), "crashed-job", result_queue),
    )
    child.start()
    child.join(timeout=10)
    assert child.exitcode == 0
    assert result_queue.get(timeout=2) is True

    receipt = cron_admission_snapshot()
    assert receipt["verified"] is True
    assert receipt["active_count"] == 0
    assert receipt["active_job_ids"] == []


def test_release_reopens_gate_and_allows_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from cron.admission import (
        claim_cron_admission,
        release_cron_admission,
        set_cron_admission_paused,
    )

    closed = set_cron_admission_paused(True, reason="test-drain")
    assert closed["accepting"] is False
    assert claim_cron_admission("blocked", source="manual") is None

    opened = set_cron_admission_paused(False, reason="test-release")
    assert opened["verified"] is True
    assert opened["accepting"] is True
    assert opened["gate_epoch"] == closed["gate_epoch"] + 1

    lease = claim_cron_admission("allowed", source="manual")
    assert lease is not None
    release_cron_admission(lease)


def test_selection_racing_marker_is_counted_before_zero_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import cron.admission as admission

    marker_active = False
    selected = threading.Event()
    marker_written = threading.Event()

    monkeypatch.setattr(
        admission,
        "_external_admission_rejection_requested",
        lambda: marker_active,
    )

    def select(_active_job_ids):
        nonlocal marker_active
        selected.set()
        marker_active = True
        marker_written.set()
        return [
            admission.CronAdmissionSelection(
                job_id="recovery-job",
                source="recovery",
                value={"id": "recovery-job"},
            )
        ]

    admitted = admission.admit_cron_selection(select)
    assert selected.is_set()
    assert marker_written.is_set()
    assert len(admitted) == 1
    value, lease = admitted[0]
    assert value == {"id": "recovery-job"}

    receipt = admission.cron_admission_snapshot()
    assert receipt["accepting"] is False
    assert receipt["active_count"] == 1
    assert receipt["active_job_ids"] == ["recovery-job"]

    admission.release_cron_admission(lease)
    assert admission.cron_admission_snapshot()["active_count"] == 0


def test_removed_job_stays_active_until_its_exact_lease_releases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from cron.admission import (
        claim_cron_admission,
        cron_admission_snapshot,
        release_cron_admission,
    )
    from cron.jobs import create_job, remove_job, use_cron_store

    with use_cron_store(tmp_path):
        job = create_job(prompt="A", schedule="every 1h", name="remove-me")
        lease = claim_cron_admission(job["id"], source="manual")
        assert lease is not None
        assert remove_job(job["id"]) is True

    receipt = cron_admission_snapshot()
    assert receipt["active_count"] == 1
    assert receipt["active_job_ids"] == [job["id"]]

    assert release_cron_admission(lease) is True
    assert cron_admission_snapshot()["active_count"] == 0


def test_unsafe_admission_lock_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from cron.admission import claim_cron_admission, cron_admission_snapshot

    cron_dir = tmp_path / "cron"
    cron_dir.mkdir(mode=0o700)
    target = tmp_path / "attacker-lock"
    target.write_text("do not follow", encoding="utf-8")
    (cron_dir / ".admission.lock").symlink_to(target)

    assert claim_cron_admission("blocked", source="manual") is None
    receipt = cron_admission_snapshot()
    assert receipt["verified"] is False
    assert receipt["accepting"] is False
    assert receipt["active_count"] is None


def test_selection_state_write_failure_discards_jobs_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import cron.admission as admission
    from cron.jobs import get_job, save_jobs, update_job, use_cron_store

    with use_cron_store(tmp_path):
        save_jobs(
            [
                {
                    "id": "atomic-job",
                    "name": "atomic-job",
                    "prompt": "x",
                    "schedule": {"kind": "interval", "minutes": 5},
                    "enabled": True,
                    "state": "scheduled",
                }
            ]
        )

        real_write = admission._secure_write_state
        writes = 0

        def fail_first_write(path, state):
            nonlocal writes
            writes += 1
            if writes == 1:
                raise admission.CronAdmissionUnavailable("injected write failure")
            return real_write(path, state)

        monkeypatch.setattr(admission, "_secure_write_state", fail_first_write)

        def select(_active):
            assert update_job("atomic-job", {"state": "running"}) is not None
            return [
                admission.CronAdmissionSelection(
                    job_id="atomic-job",
                    source="builtin_due",
                    value="selected",
                )
            ]

        assert admission.admit_cron_selection(select) == []
        assert get_job("atomic-job")["state"] == "scheduled"
        assert admission.cron_admission_snapshot()["active_count"] == 0


def test_invalid_selection_discards_complete_batch_and_store_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import cron.admission as admission
    from cron.jobs import get_job, save_jobs, update_job, use_cron_store

    with use_cron_store(tmp_path):
        save_jobs(
            [
                {
                    "id": "batch-job",
                    "name": "batch-job",
                    "prompt": "x",
                    "schedule": {"kind": "interval", "minutes": 5},
                    "enabled": True,
                    "state": "scheduled",
                }
            ]
        )

        def select(_active):
            assert update_job("batch-job", {"state": "running"}) is not None
            return [
                admission.CronAdmissionSelection(
                    job_id="batch-job",
                    source="builtin_due",
                    value="selected",
                ),
                object(),
            ]

        assert admission.admit_cron_selection(select) == []
        assert get_job("batch-job")["state"] == "scheduled"
        assert admission.cron_admission_snapshot()["active_count"] == 0


def test_jobs_commit_failure_discards_buffer_and_releases_persisted_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import cron.admission as admission
    import cron.jobs as jobs

    with jobs.use_cron_store(tmp_path):
        jobs.save_jobs(
            [
                {
                    "id": "commit-failure",
                    "name": "commit-failure",
                    "prompt": "x",
                    "schedule": {"kind": "interval", "minutes": 5},
                    "enabled": True,
                    "state": "scheduled",
                }
            ]
        )

        monkeypatch.setattr(
            jobs._BufferedJobsTransaction,
            "commit",
            lambda _self: (_ for _ in ()).throw(OSError("injected commit failure")),
        )

        def select(_active):
            assert jobs.update_job(
                "commit-failure",
                {"state": "running"},
            ) is not None
            return [
                admission.CronAdmissionSelection(
                    job_id="commit-failure",
                    source="builtin_due",
                    value="selected",
                )
            ]

        with pytest.raises(OSError, match="injected commit failure"):
            admission.admit_cron_selection(select)

        assert jobs.get_job("commit-failure")["state"] == "scheduled"
        assert admission.cron_admission_snapshot()["active_count"] == 0


def test_release_retries_transient_persistence_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import cron.admission as admission

    lease = admission.claim_cron_admission("retry-release", source="test")
    assert lease is not None
    real_release = admission._release_cron_admission_once
    attempts = 0

    def flaky_release(candidate):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise admission.CronAdmissionUnavailable("transient")
        return real_release(candidate)

    monkeypatch.setattr(
        admission,
        "_release_cron_admission_once",
        flaky_release,
    )
    assert admission.release_cron_admission(lease) is True
    assert attempts == 3
    assert admission.cron_admission_snapshot()["active_count"] == 0


def test_release_retry_worker_settles_after_synchronous_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import cron.admission as admission

    lease = admission.claim_cron_admission("background-release", source="test")
    assert lease is not None
    real_release = admission._release_cron_admission_once
    attempts = 0
    settled = threading.Event()

    def delayed_release(candidate):
        nonlocal attempts
        attempts += 1
        if attempts <= 4:
            raise admission.CronAdmissionUnavailable("transient")
        result = real_release(candidate)
        settled.set()
        return result

    monkeypatch.setattr(
        admission,
        "_release_cron_admission_once",
        delayed_release,
    )
    assert admission.release_cron_admission(lease) is False
    assert settled.wait(timeout=2)
    assert attempts >= 5
    assert admission.cron_admission_snapshot()["active_count"] == 0
