"""Regression tests for persisted cron budget-exhaustion recovery."""

from cron.recovery import (
    _recovery_path,
    clear_recovery_record,
    get_recovery_record,
    save_recovery_record,
    should_auto_pause,
)
from cron.scheduler import _build_job_prompt, run_job
import cron.jobs as jobs


def test_recovery_record_lifecycle_and_auto_pause_threshold():
    job_id = "abc123"

    assert get_recovery_record(job_id) is None

    first = save_recovery_record(
        job_id,
        recovery_text="RECOVERY_REQUIRED:\nverify first",
        budget_used=90,
        budget_max=90,
        changed_paths=["foo.py"],
    )
    assert first["consecutive_exhaustions"] == 1
    assert first["changed_paths"] == ["foo.py"]
    assert should_auto_pause(first) is False

    second = save_recovery_record(
        job_id,
        recovery_text="RECOVERY_REQUIRED:\nverify second",
        budget_used=90,
        budget_max=90,
    )
    third = save_recovery_record(
        job_id,
        recovery_text="RECOVERY_REQUIRED:\nverify third",
        budget_used=90,
        budget_max=90,
    )

    assert second["consecutive_exhaustions"] == 2
    assert third["consecutive_exhaustions"] == 3
    assert should_auto_pause(third) is True

    clear_recovery_record(job_id)
    assert get_recovery_record(job_id) is None


def test_corrupt_recovery_record_is_ignored():
    job_id = "abc123"
    save_recovery_record(
        job_id,
        recovery_text="RECOVERY_REQUIRED:\nverify first",
        budget_used=90,
        budget_max=90,
    )
    path = _recovery_path(job_id)
    path.write_text("{not-json", encoding="utf-8")

    assert get_recovery_record(job_id) is None


def test_build_job_prompt_injects_recovery_context_before_original_prompt():
    job = {
        "id": "abc123",
        "name": "Budget Test",
        "prompt": "Original scheduled work",
        "schedule": "every 1h",
    }
    save_recovery_record(
        job["id"],
        recovery_text="RECOVERY_REQUIRED:\nrun verification first",
        budget_used=90,
        budget_max=90,
    )

    prompt = _build_job_prompt(job)

    assert prompt.startswith("[IMPORTANT: You are running as a scheduled cron job")
    assert "Recovering from Budget Exhaustion" in prompt
    assert "RECOVERY_REQUIRED:" in prompt
    assert prompt.index("Recovering from Budget Exhaustion") < prompt.index(
        "Original scheduled work"
    )


def test_run_job_auto_pauses_after_three_consecutive_exhaustions_without_model_call():
    job = jobs.create_job(
        prompt="SHOULD_NOT_RUN_MODEL",
        schedule="every 1h",
        name="Circuit Test",
        deliver="local",
    )
    for idx in range(3):
        save_recovery_record(
            job["id"],
            recovery_text=f"RECOVERY_REQUIRED {idx}",
            budget_used=90,
            budget_max=90,
        )

    success, doc, response, err = run_job(job)

    assert success is False
    assert err == "budget_exhaustion_circuit_breaker"
    assert "auto-paused" in response
    assert "SHOULD_NOT_RUN_MODEL" not in response
    assert "AUTO-PAUSED" in doc
    updated = jobs.get_job(job["id"])
    assert updated["enabled"] is False
    assert updated.get("state") == "paused"
    assert get_recovery_record(job["id"]) is not None
