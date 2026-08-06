"""Focused tests for persisted cron recovery state."""

from datetime import datetime, timedelta, timezone

import pytest

import cron.jobs as jobs
import cron.scheduler as scheduler


@pytest.fixture()
def tmp_cron_recovery(tmp_path, monkeypatch):
    """Redirect cron storage and scheduler lock paths to a temp tree."""
    cron_dir = tmp_path / "cron"
    monkeypatch.setattr(jobs, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", cron_dir / "output")
    monkeypatch.setattr(scheduler, "_hermes_home", tmp_path)
    return tmp_path


def _force_retry_due(job_id: str) -> None:
    stored = jobs.load_jobs()
    for job in stored:
        if job["id"] == job_id:
            job["next_retry_at"] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
            break
    jobs.save_jobs(stored)


class TestCronRecoveryState:
    def test_delivery_failure_schedules_artifact_replay_without_removing_oneshot(self, tmp_cron_recovery):
        job = jobs.create_job(prompt="Report", schedule="1h", deliver="local")
        output_file = jobs.save_job_output(
            job["id"],
            "# Cron Job: Report\n\n## Response\n\nhello from saved output\n",
        )

        jobs.mark_job_run(
            job["id"],
            success=True,
            delivery_error="telegram send failed",
            output_file=output_file,
        )

        updated = jobs.get_job(job["id"])
        assert updated is not None
        assert updated["last_status"] == "ok"
        assert updated["last_delivery_error"] == "telegram send failed"
        assert updated["recovery_state"] == jobs.RECOVERY_STATE_SCHEDULED
        assert updated["failure_class"] == jobs.FAILURE_CLASS_DELIVERY
        assert updated["manual_action_required"] is False
        assert updated["recovery_output_file"] == str(output_file)
        assert updated["repeat"]["completed"] == 1

    def test_inference_failure_requires_opt_in_for_rerun(self, tmp_cron_recovery):
        job = jobs.create_job(prompt="Report", schedule="every 1h", deliver="local")

        jobs.mark_job_run(job["id"], success=False, error="ReadTimeout: provider stalled")

        updated = jobs.get_job(job["id"])
        assert updated["recovery_state"] == jobs.RECOVERY_STATE_MANUAL
        assert updated["manual_action_required"] is True
        assert updated["state"] == "paused"
        assert "allow_recovery_rerun=True" in updated["paused_reason"]

    def test_opted_in_inference_failure_gets_bounded_retry_state(self, tmp_cron_recovery):
        job = jobs.create_job(
            prompt="Report",
            schedule="every 1h",
            deliver="local",
            allow_recovery_rerun=True,
        )

        jobs.mark_job_run(job["id"], success=False, error="ReadTimeout: provider stalled")

        updated = jobs.get_job(job["id"])
        assert updated["recovery_state"] == jobs.RECOVERY_STATE_SCHEDULED
        assert updated["failure_class"] == jobs.FAILURE_CLASS_INFERENCE
        assert updated["retry_attempt"] == 0
        assert updated["next_retry_at"]
        assert updated["manual_action_required"] is False
        assert updated["enabled"] is True

    def test_claim_due_recovery_is_atomic_and_increments_budget_once(self, tmp_cron_recovery):
        job = jobs.create_job(prompt="Report", schedule="every 1h", deliver="local")
        output_file = jobs.save_job_output(job["id"], "# Cron Job\n\n## Response\n\nhello")
        jobs.mark_job_run(job["id"], success=True, delivery_error="network down", output_file=output_file)
        _force_retry_due(job["id"])

        first = jobs.claim_due_recovery_jobs()
        second = jobs.claim_due_recovery_jobs()

        assert [j["id"] for j in first] == [job["id"]]
        assert second == []
        updated = jobs.get_job(job["id"])
        assert updated["recovery_state"] == jobs.RECOVERY_STATE_RUNNING
        assert updated["retry_attempt"] == 1
        assert updated["recovery_claim"]["token"] == first[0]["recovery_claim"]["token"]

    def test_retry_budget_exhaustion_pauses_for_operator(self, tmp_cron_recovery):
        job = jobs.create_job(prompt="Report", schedule="every 1h", deliver="local")
        output_file = jobs.save_job_output(job["id"], "# Cron Job\n\n## Response\n\nhello")
        jobs.mark_job_run(job["id"], success=True, delivery_error="network down", output_file=output_file)

        stored = jobs.load_jobs()
        stored[0]["retry_attempt"] = 3
        stored[0]["next_retry_at"] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        jobs.save_jobs(stored)

        assert jobs.claim_due_recovery_jobs() == []
        updated = jobs.get_job(job["id"])
        assert updated["recovery_state"] == jobs.RECOVERY_STATE_MANUAL
        assert updated["manual_action_required"] is True
        assert updated["enabled"] is False
        assert updated["state"] == "paused"

    def test_pending_recovery_holds_base_schedule_without_advancing_next_run(self, tmp_cron_recovery):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        job = jobs.create_job(prompt="Report", schedule="every 1h", deliver="local")
        output_file = jobs.save_job_output(job["id"], "# Cron Job\n\n## Response\n\nhello")
        jobs.mark_job_run(job["id"], success=True, delivery_error="network down", output_file=output_file)

        stored = jobs.load_jobs()
        original_next = (now - timedelta(minutes=5)).isoformat()
        stored[0]["next_run_at"] = original_next
        jobs.save_jobs(stored)

        due = jobs.get_due_jobs()

        assert due == []
        assert jobs.get_job(job["id"])["next_run_at"] == original_next


class TestCronRecoveryScheduler:
    def test_delivery_replay_uses_saved_output_without_rerunning_job(self, tmp_cron_recovery, monkeypatch):
        job = jobs.create_job(prompt="Report", schedule="every 1h", deliver="local")
        output_file = jobs.save_job_output(
            job["id"],
            "# Cron Job: Report\n\n## Response\n\nhello from saved output\n",
        )
        jobs.mark_job_run(
            job["id"],
            success=True,
            delivery_error="network down",
            output_file=output_file,
        )
        _force_retry_due(job["id"])

        delivered = []

        def fake_deliver(_job, content, adapters=None, loop=None):
            delivered.append(content)
            return None

        def fail_if_rerun(_job):  # pragma: no cover - failure path is the assertion
            raise AssertionError("delivery recovery reran the job")

        monkeypatch.setattr(scheduler, "_deliver_result", fake_deliver)
        monkeypatch.setattr(scheduler, "run_job", fail_if_rerun)

        assert scheduler.tick(verbose=False, sync=True) == 1

        assert delivered == ["hello from saved output"]
        updated = jobs.get_job(job["id"])
        assert updated["recovery_state"] == jobs.RECOVERY_STATE_IDLE
        assert updated["last_delivery_error"] is None

    def test_opted_in_rerun_preserves_canonical_next_run(self, tmp_cron_recovery, monkeypatch):
        job = jobs.create_job(
            prompt="Report",
            schedule="every 1h",
            deliver="local",
            allow_recovery_rerun=True,
        )
        jobs.mark_job_run(job["id"], success=False, error="ReadTimeout: provider stalled")
        _force_retry_due(job["id"])
        next_after_failure = jobs.get_job(job["id"])["next_run_at"]

        def fake_run_job(_job):
            return True, "# Cron Job: Report\n\n## Response\n\nrerun ok\n", "rerun ok", None

        monkeypatch.setattr(scheduler, "run_job", fake_run_job)
        monkeypatch.setattr(scheduler, "_deliver_result", lambda *args, **kwargs: None)

        assert scheduler.tick(verbose=False, sync=True) == 1

        updated = jobs.get_job(job["id"])
        assert updated["recovery_state"] == jobs.RECOVERY_STATE_IDLE
        assert updated["last_status"] == "ok"
        assert updated["next_run_at"] == next_after_failure
