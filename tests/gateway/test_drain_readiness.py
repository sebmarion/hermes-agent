"""Fail-closed gateway admission and quiescence receipt semantics."""

from gateway.drain_readiness import build_drain_readiness


def test_rejecting_zero_work_is_verified_quiescence():
    receipt = build_drain_readiness(
        live_admission_rejecting=True,
        drain_requested=True,
        active_http_requests=0,
        active_agent_turns=0,
        active_delegations=0,
        background_processes=0,
        process_completion_queue_depth=0,
        active_cron_jobs=0,
        api_background_tasks=0,
        running_kanban_workers=0,
        gateway_background_tasks=0,
    )

    assert receipt["schema"] == "hermes.gateway_drain.v1"
    assert receipt["admission"] == {
        "state": "rejecting_new_work",
        "verified": True,
        "drain_requested": True,
    }
    assert receipt["work"] == {
        "active_http_requests": 0,
        "active_agent_turns": 0,
        "active_delegations": 0,
        "background_processes": 0,
        "process_completion_queue_depth": 0,
        "active_cron_jobs": 0,
        "api_background_tasks": 0,
        "running_kanban_workers": 0,
        "gateway_background_tasks": 0,
    }
    assert receipt["quiescence"] == {
        "verified": True,
        "quiescent": True,
        "blockers": [],
    }


def test_marker_before_runner_ack_is_transitioning_not_verified():
    receipt = build_drain_readiness(
        live_admission_rejecting=False,
        drain_requested=True,
        active_http_requests=0,
        active_agent_turns=0,
        active_delegations=0,
        background_processes=0,
        process_completion_queue_depth=0,
        active_cron_jobs=0,
        api_background_tasks=0,
        running_kanban_workers=0,
        gateway_background_tasks=0,
    )

    assert receipt["admission"]["state"] == "transitioning_to_reject"
    assert receipt["admission"]["verified"] is False
    assert receipt["quiescence"]["verified"] is False
    assert receipt["quiescence"]["quiescent"] is False
    assert "admission_unverified" in receipt["quiescence"]["blockers"]


def test_unverified_pair_gate_fails_quiescence_closed_even_when_runner_rejects():
    receipt = build_drain_readiness(
        live_admission_rejecting=True,
        drain_requested=False,
        active_http_requests=0,
        active_agent_turns=0,
        active_delegations=0,
        background_processes=0,
        process_completion_queue_depth=0,
        active_cron_jobs=0,
        api_background_tasks=0,
        running_kanban_workers=0,
        gateway_background_tasks=0,
        pair_open_gate={
            "active": True,
            "verified": False,
            "reason": "invalid_payload",
        },
    )

    assert receipt["admission"]["state"] == "rejecting_new_work"
    assert receipt["admission"]["effective_rejection_requested"] is True
    assert receipt["quiescence"]["verified"] is False
    assert receipt["quiescence"]["quiescent"] is False
    assert receipt["quiescence"]["blockers"] == [
        "pair_open_gate_unverified"
    ]


def test_active_work_prevents_quiescence_with_exact_blockers():
    receipt = build_drain_readiness(
        live_admission_rejecting=True,
        drain_requested=True,
        active_http_requests=2,
        active_agent_turns=1,
        active_delegations=3,
        background_processes=4,
        process_completion_queue_depth=5,
        active_cron_jobs=6,
        api_background_tasks=7,
        running_kanban_workers=8,
        gateway_background_tasks=9,
    )

    assert receipt["quiescence"]["verified"] is True
    assert receipt["quiescence"]["quiescent"] is False
    assert receipt["quiescence"]["blockers"] == [
        "active_http_requests",
        "active_agent_turns",
        "active_delegations",
        "background_processes",
        "process_completion_queue_depth",
        "active_cron_jobs",
        "api_background_tasks",
        "running_kanban_workers",
        "gateway_background_tasks",
    ]


def test_unavailable_work_source_fails_quiescence_closed():
    receipt = build_drain_readiness(
        live_admission_rejecting=True,
        drain_requested=True,
        active_http_requests=0,
        active_agent_turns=0,
        active_delegations=None,
        background_processes=0,
        process_completion_queue_depth=0,
        active_cron_jobs=0,
        api_background_tasks=0,
        running_kanban_workers=0,
        gateway_background_tasks=0,
    )

    assert receipt["work"]["active_delegations"] is None
    assert receipt["work_status"]["active_delegations"] == "unverified"
    assert receipt["quiescence"]["verified"] is False
    assert receipt["quiescence"]["quiescent"] is False
    assert receipt["quiescence"]["blockers"] == [
        "active_delegations_unverified"
    ]


def test_pending_process_completion_blocks_quiescence():
    receipt = build_drain_readiness(
        live_admission_rejecting=True,
        drain_requested=True,
        active_http_requests=0,
        active_agent_turns=0,
        active_delegations=0,
        background_processes=0,
        process_completion_queue_depth=1,
        active_cron_jobs=0,
        api_background_tasks=0,
        running_kanban_workers=0,
        gateway_background_tasks=0,
    )

    assert receipt["work"]["process_completion_queue_depth"] == 1
    assert receipt["quiescence"]["verified"] is True
    assert receipt["quiescence"]["quiescent"] is False
    assert receipt["quiescence"]["blockers"] == [
        "process_completion_queue_depth"
    ]


def test_active_cron_job_blocks_quiescence():
    receipt = build_drain_readiness(
        live_admission_rejecting=True,
        drain_requested=True,
        active_http_requests=0,
        active_agent_turns=0,
        active_delegations=0,
        background_processes=0,
        process_completion_queue_depth=0,
        active_cron_jobs=1,
        api_background_tasks=0,
        running_kanban_workers=0,
        gateway_background_tasks=0,
    )

    assert receipt["work"]["active_cron_jobs"] == 1
    assert receipt["quiescence"]["quiescent"] is False
    assert receipt["quiescence"]["blockers"] == ["active_cron_jobs"]


def test_missing_live_admission_state_fails_closed():
    receipt = build_drain_readiness(
        live_admission_rejecting=None,
        drain_requested=True,
        active_http_requests=0,
        active_agent_turns=0,
        active_delegations=0,
        background_processes=0,
        process_completion_queue_depth=0,
        active_cron_jobs=0,
        api_background_tasks=0,
        running_kanban_workers=0,
        gateway_background_tasks=0,
    )

    assert receipt["admission"]["state"] == "unknown"
    assert receipt["admission"]["verified"] is False
    assert receipt["quiescence"]["quiescent"] is False


def test_api_background_task_and_detached_kanban_worker_block_quiescence():
    receipt = build_drain_readiness(
        live_admission_rejecting=True,
        drain_requested=True,
        active_http_requests=0,
        active_agent_turns=0,
        active_delegations=0,
        background_processes=0,
        process_completion_queue_depth=0,
        active_cron_jobs=0,
        api_background_tasks=1,
        running_kanban_workers=2,
        gateway_background_tasks=0,
    )

    assert receipt["quiescence"]["verified"] is True
    assert receipt["quiescence"]["quiescent"] is False
    assert receipt["quiescence"]["blockers"] == [
        "api_background_tasks",
        "running_kanban_workers",
    ]
