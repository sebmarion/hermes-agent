"""Tests for #60432: cron jobs must not be silently invisible to gateway
shutdown, and a job whose tool subprocess got killed by shutdown must
never be reported as a successful run.

Covers the cron/scheduler.py primitives directly:
  - get_running_job_ids() -- thread-safe snapshot the gateway drain reads
  - mark_running_jobs_interrupted() -- called by the gateway right after
    it force-kills tool subprocesses
  - the interrupted-flag race guard in run_one_job(), which must win over
    the job's own thread finishing normally with a plausible-looking
    result AFTER its tool was already killed out from under it
"""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _reset_scheduler_state():
    """Every test starts from a clean slate and leaves one behind, since
    these sets are module-level globals shared across the test process."""
    import cron.scheduler as sched

    sched._running_job_ids.clear()
    sched._running_dispatch_keys.clear()
    sched._owned_admission_leases.clear()
    sched._dispatch_paused_profiles.clear()
    sched._interrupted_job_ids.clear()
    sched._dispatch_paused_for_drain = False
    yield
    sched._running_job_ids.clear()
    sched._running_dispatch_keys.clear()
    sched._owned_admission_leases.clear()
    sched._dispatch_paused_profiles.clear()
    sched._interrupted_job_ids.clear()
    sched._dispatch_paused_for_drain = False


class TestGetRunningJobIds:
    def test_empty_when_nothing_running(self):
        import cron.scheduler as sched

        assert sched.get_running_job_ids() == frozenset()

    def test_reflects_in_flight_jobs(self):
        import cron.scheduler as sched

        sched._running_job_ids.add("job-1")
        sched._running_job_ids.add("job-2")

        result = sched.get_running_job_ids()

        assert result == frozenset({"job-1", "job-2"})

    def test_snapshot_is_immutable_and_independent(self):
        """Mutating _running_job_ids after the call must not change the
        already-returned snapshot -- callers (the gateway drain loop) rely
        on this to safely count in a tight polling loop."""
        import cron.scheduler as sched

        sched._running_job_ids.add("job-1")
        snapshot = sched.get_running_job_ids()
        sched._running_job_ids.add("job-2")

        assert snapshot == frozenset({"job-1"})


class TestCronDrainAdmission:
    def test_same_job_id_is_isolated_across_profiles(self, tmp_path):
        import cron.scheduler as sched
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        home_a = tmp_path / "profile-a"
        home_b = tmp_path / "profile-b"
        sched._hermes_home = None

        token_a = set_hermes_home_override(home_a)
        try:
            assert sched._try_claim_cron_dispatch("shared-id") is True
            assert sched.get_running_job_ids() == frozenset({"shared-id"})

            token_b = set_hermes_home_override(home_b)
            try:
                assert sched._try_claim_cron_dispatch("shared-id") is True
                assert sched.get_running_job_ids() == frozenset({"shared-id"})
                sched._release_cron_dispatch("shared-id")
                assert sched.get_running_job_ids() == frozenset()
            finally:
                reset_hermes_home_override(token_b)

            assert sched.get_running_job_ids() == frozenset({"shared-id"})
            sched._release_cron_dispatch("shared-id")
            assert sched.get_running_job_ids() == frozenset()
        finally:
            reset_hermes_home_override(token_a)

    def test_paused_tick_does_not_claim_or_advance_store_state(
        self, tmp_path, monkeypatch
    ):
        """The fence must precede every recovery/due jobs.json mutation."""
        import cron.scheduler as sched

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(sched, "_hermes_home", tmp_path)
        sched.set_cron_dispatch_paused(True)

        touched = []
        monkeypatch.setattr(
            sched,
            "claim_due_recovery_jobs",
            lambda: touched.append("recovery_claim") or [],
        )
        monkeypatch.setattr(
            sched,
            "get_due_jobs",
            lambda: touched.append("due_claim") or [],
        )
        monkeypatch.setattr(
            sched,
            "advance_next_run",
            lambda *_a, **_kw: touched.append("advance"),
        )

        assert sched.tick(verbose=False, sync=True) == 0
        assert touched == []

    def test_recovery_run_is_in_shared_active_receipt(
        self, tmp_path, monkeypatch
    ):
        import cron.scheduler as sched
        from cron.admission import cron_admission_snapshot

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(sched, "_hermes_home", tmp_path)
        job = {"id": "recovering", "name": "recovering"}
        observed = []

        monkeypatch.setattr(
            sched, "claim_due_recovery_jobs", lambda **_kw: [job]
        )
        monkeypatch.setattr(sched, "get_due_jobs", lambda: [])
        monkeypatch.setattr(
            sched,
            "_run_recovery_job",
            lambda *_a, **_kw: observed.append(cron_admission_snapshot()) or True,
        )

        assert sched.tick(verbose=False, sync=True) == 1
        assert observed[0]["active_count"] == 1
        assert observed[0]["active_job_ids"] == ["recovering"]
        assert cron_admission_snapshot()["active_count"] == 0

    def test_due_no_agent_job_is_counted_while_queued_and_running(
        self, tmp_path, monkeypatch
    ):
        import cron.scheduler as sched
        from cron.admission import cron_admission_snapshot

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(sched, "_hermes_home", tmp_path)
        job = {
            "id": "script-only",
            "name": "script-only",
            "no_agent": True,
            "script": "probe.py",
            "workdir": "",
        }
        observed = []

        monkeypatch.setattr(
            sched, "claim_due_recovery_jobs", lambda **_kw: []
        )
        monkeypatch.setattr(sched, "get_due_jobs", lambda: [job])
        monkeypatch.setattr(sched, "advance_next_run", lambda *_a, **_kw: None)
        monkeypatch.setattr(sched, "load_config", lambda: {})
        monkeypatch.setattr(
            sched,
            "run_one_job",
            lambda *_a, **_kw: observed.append(cron_admission_snapshot()) or True,
        )

        assert sched.tick(verbose=False, sync=True) == 1
        assert observed[0]["active_count"] == 1
        assert observed[0]["active_job_ids"] == ["script-only"]
        assert cron_admission_snapshot()["active_count"] == 0

    def test_pause_blocks_direct_dispatch_before_any_side_effect(self):
        import cron.scheduler as sched

        sched.set_cron_dispatch_paused(True)

        with patch("cron.scheduler.claim_dispatch") as claim:
            result = sched.run_one_job({"id": "job-1", "name": "blocked"})

        assert result is False
        claim.assert_not_called()
        assert sched.get_running_job_ids() == frozenset()

    def test_executor_submit_failure_releases_dispatch_claim(
        self, tmp_path, monkeypatch
    ):
        """A rejected submit must not leave drain readiness wedged forever."""
        import cron.scheduler as sched
        from gateway.platforms.api_server import APIServerAdapter

        job = {
            "id": "submit-failed",
            "name": "submit-failed",
            "prompt": "test",
            "schedule": "every 5m",
            "enabled": True,
            "next_run_at": "2020-01-01T00:00:00",
            "deliver": "local",
        }

        class RejectingPool:
            def submit(self, *args, **kwargs):
                raise RuntimeError("executor is broken")

        monkeypatch.setattr(sched, "_hermes_home", tmp_path)
        monkeypatch.setattr(
            sched, "claim_due_recovery_jobs", lambda **_kw: []
        )
        monkeypatch.setattr(sched, "get_due_jobs", lambda: [job])
        monkeypatch.setattr(sched, "advance_next_run", lambda *_a, **_kw: None)
        monkeypatch.setattr(sched, "load_config", lambda: {})
        monkeypatch.setattr(sched, "_get_parallel_pool", lambda *_a, **_kw: RejectingPool())
        monkeypatch.setattr(sched, "_interpreter_shutting_down", lambda *_a, **_kw: False)

        with pytest.raises(RuntimeError, match="executor is broken"):
            sched.tick(verbose=False, sync=False)

        assert sched.get_running_job_ids() == frozenset()

        api_adapter = object.__new__(APIServerAdapter)
        api_adapter._run_statuses = {}
        api_adapter._drain_tracked_tasks = set()
        monkeypatch.setattr(
            "gateway.platforms.api_server._count_running_kanban_workers",
            lambda: 0,
        )
        assert api_adapter._readiness_work_counts()["active_cron_jobs"] == 0

        assert sched._try_claim_cron_dispatch(job["id"]) is True
        sched._release_cron_dispatch(job["id"])

    def test_resume_allows_direct_dispatch_and_tracks_entire_run(self):
        import cron.scheduler as sched

        observed = []

        def _run_job(*args, **kwargs):
            observed.append(sched.get_running_job_ids())
            return True, "out", "done", None

        sched.set_cron_dispatch_paused(False)
        with patch("cron.scheduler.claim_dispatch", return_value=True), \
             patch("agent.secret_scope.set_secret_scope", return_value=None), \
             patch("agent.secret_scope.build_profile_secret_scope", return_value=None), \
             patch("agent.secret_scope.reset_secret_scope"), \
             patch("cron.scheduler.run_job", side_effect=_run_job), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._is_cron_silence_response", return_value=False), \
             patch("cron.scheduler._deliver_result", return_value=None), \
             patch("cron.scheduler.mark_job_run"):
            result = sched.run_one_job({"id": "job-1", "name": "allowed"})

        assert result is True
        assert observed == [frozenset({"job-1"})]
        assert sched.get_running_job_ids() == frozenset()

    def test_resume_keeps_local_fast_path_closed_while_marker_still_fences(
        self,
        tmp_path,
        monkeypatch,
    ):
        import cron.admission as admission
        import cron.scheduler as sched

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(
            admission,
            "_external_admission_rejection_requested",
            lambda: True,
        )

        sched._dispatch_paused_for_drain = True
        with pytest.raises(RuntimeError, match="remains fenced"):
            sched.set_cron_dispatch_paused(False)

        assert sched.get_cron_admission_receipt()["accepting"] is False
        assert sched._dispatch_paused_for_drain is True


class TestMarkRunningJobsInterrupted:
    def test_no_op_when_nothing_running(self):
        import cron.scheduler as sched

        with patch("cron.scheduler.mark_job_run") as mock_mark:
            marked = sched.mark_running_jobs_interrupted("shutdown")

        assert marked == []
        mock_mark.assert_not_called()

    def test_marks_every_in_flight_job(self):
        import cron.scheduler as sched

        sched._running_job_ids.update({"job-1", "job-2"})

        with patch("cron.scheduler.mark_job_run") as mock_mark:
            marked = sched.mark_running_jobs_interrupted("gateway shutdown (final-cleanup)")

        assert sorted(marked) == ["job-1", "job-2"]
        assert mock_mark.call_count == 2
        called_ids = {c.args[0] for c in mock_mark.call_args_list}
        assert called_ids == {"job-1", "job-2"}
        for c in mock_mark.call_args_list:
            # success must be False -- an interrupted run is never "ok".
            assert c.args[1] is False
            assert "gateway shutdown" in c.args[2]

    def test_sets_interrupted_flag_for_consumption_by_run_one_job(self):
        import cron.scheduler as sched

        sched._running_job_ids.add("job-1")

        with patch("cron.scheduler.mark_job_run"):
            sched.mark_running_jobs_interrupted("shutdown")

        assert "job-1" in sched._interrupted_job_ids

    def test_one_job_marking_failure_does_not_block_the_others(self):
        """mark_job_run raising for one job (e.g. a jobs.json write race)
        must not prevent the rest from being marked -- this runs during
        shutdown, there's no retry window."""
        import cron.scheduler as sched

        sched._running_job_ids.update({"job-1", "job-2"})

        def _side_effect(job_id, success, reason, **kwargs):
            if job_id == "job-1":
                raise OSError("disk full")

        with patch("cron.scheduler.mark_job_run", side_effect=_side_effect):
            marked = sched.mark_running_jobs_interrupted("shutdown")

        assert marked == ["job-2"]


class TestIsInterrupted:
    """Peek-only check used at the delivery gate -- must NOT clear the
    flag, unlike _consume_interrupted_flag."""

    def test_false_when_not_marked(self):
        import cron.scheduler as sched

        assert sched._is_interrupted("job-1") is False

    def test_true_when_marked(self):
        import cron.scheduler as sched

        sched._interrupted_job_ids.add("job-1")

        assert sched._is_interrupted("job-1") is True

    def test_does_not_clear_the_flag(self):
        import cron.scheduler as sched

        sched._interrupted_job_ids.add("job-1")

        sched._is_interrupted("job-1")

        # Still set -- the later, authoritative check before mark_job_run
        # must still see it.
        assert "job-1" in sched._interrupted_job_ids
        assert sched._is_interrupted("job-1") is True


class TestConsumeInterruptedFlag:
    def test_false_when_not_marked(self):
        import cron.scheduler as sched

        assert sched._consume_interrupted_flag("job-1") is False

    def test_true_and_clears_when_marked(self):
        import cron.scheduler as sched

        sched._interrupted_job_ids.add("job-1")

        assert sched._consume_interrupted_flag("job-1") is True
        # Consumed -- a second check (e.g. a later, unrelated fire of the
        # same recurring job ID) must not still read as interrupted.
        assert sched._consume_interrupted_flag("job-1") is False


class TestRunOneJobHonoursInterruptedFlag:
    """run_one_job() must not let a job's own completion overwrite a
    status the shutdown path already wrote for the same run."""

    def _make_job(self, job_id="job-1"):
        return {"id": job_id, "name": "test job", "prompt": "do work"}

    def test_success_path_skipped_when_interrupted(self):
        import cron.scheduler as sched

        job = self._make_job()
        sched._interrupted_job_ids.add(job["id"])

        with patch("cron.scheduler.claim_dispatch", return_value=True), \
             patch("agent.secret_scope.set_secret_scope", return_value=None), \
             patch("agent.secret_scope.build_profile_secret_scope", return_value=None), \
             patch("agent.secret_scope.reset_secret_scope"), \
             patch(
                 "cron.scheduler.run_job",
                 return_value=(True, "full output", "final response", None),
             ), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._is_cron_silence_response", return_value=False), \
             patch("cron.scheduler._deliver_result", return_value=None), \
             patch("cron.scheduler.mark_job_run") as mock_mark:
            result = sched.run_one_job(job)

        assert result is True
        # The would-be "success" write must NOT happen -- the shutdown
        # path already wrote the authoritative interrupted status.
        mock_mark.assert_not_called()
        # Flag is consumed so a later, unrelated fire of the same job ID
        # isn't permanently silenced.
        assert job["id"] not in sched._interrupted_job_ids

    def test_interrupted_job_delivers_failure_summary_not_raw_response(self):
        """The status-write guard alone isn't enough: delivery happens
        BEFORE mark_job_run in run_one_job's own flow, so a job that kept
        running post-kill and produced a plausible-looking final_response
        must not have that response sent to the user just because the
        eventual status write gets suppressed. Interrupted jobs must route
        through the same failure-summary delivery path a real failure
        would."""
        import cron.scheduler as sched

        job = self._make_job()
        sched._interrupted_job_ids.add(job["id"])

        with patch("cron.scheduler.claim_dispatch", return_value=True), \
             patch("agent.secret_scope.set_secret_scope", return_value=None), \
             patch("agent.secret_scope.build_profile_secret_scope", return_value=None), \
             patch("agent.secret_scope.reset_secret_scope"), \
             patch(
                 "cron.scheduler.run_job",
                 return_value=(True, "full output", "a plausible final response", None),
             ), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch(
                 "cron.scheduler._summarize_cron_failure_for_delivery",
                 return_value="This run was interrupted.",
             ) as mock_summarize, \
             patch("cron.scheduler._is_cron_silence_response", return_value=False), \
             patch("cron.scheduler._deliver_result", return_value=None) as mock_deliver, \
             patch("cron.scheduler.mark_job_run"):
            result = sched.run_one_job(job)

        assert result is True
        mock_summarize.assert_called_once()
        # The summarizer's error argument must mention the interruption,
        # not be silently None / the agent's own (possibly absent) error.
        assert "interrupt" in mock_summarize.call_args.args[1].lower()
        delivered_content = mock_deliver.call_args.args[1]
        assert delivered_content == "This run was interrupted."
        assert "plausible final response" not in delivered_content

    def test_success_path_writes_normally_when_not_interrupted(self):
        """Control case: the guard must not swallow ordinary, un-interrupted
        completions -- only ones the shutdown path explicitly flagged."""
        import cron.scheduler as sched

        job = self._make_job()

        with patch("cron.scheduler.claim_dispatch", return_value=True), \
             patch("agent.secret_scope.set_secret_scope", return_value=None), \
             patch("agent.secret_scope.build_profile_secret_scope", return_value=None), \
             patch("agent.secret_scope.reset_secret_scope"), \
             patch(
                 "cron.scheduler.run_job",
                 return_value=(True, "full output", "final response", None),
             ), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._is_cron_silence_response", return_value=False), \
             patch("cron.scheduler._deliver_result", return_value=None), \
             patch("cron.scheduler.mark_job_run") as mock_mark:
            result = sched.run_one_job(job)

        assert result is True
        mock_mark.assert_called_once()
        assert mock_mark.call_args.args[0] == job["id"]
        assert mock_mark.call_args.args[1] is True  # success

    def test_exception_path_also_honours_interrupted_flag(self):
        import cron.scheduler as sched

        job = self._make_job()
        sched._interrupted_job_ids.add(job["id"])

        with patch("cron.scheduler.claim_dispatch", return_value=True), \
             patch("agent.secret_scope.set_secret_scope", return_value=None), \
             patch("agent.secret_scope.build_profile_secret_scope", return_value=None), \
             patch("agent.secret_scope.reset_secret_scope"), \
             patch("cron.scheduler.run_job", side_effect=RuntimeError("boom")), \
             patch("cron.scheduler.mark_job_run") as mock_mark:
            result = sched.run_one_job(job)

        assert result is False
        mock_mark.assert_not_called()
