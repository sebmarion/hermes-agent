from agent.bestplan_orchestrator import RECEIPT_VERSION, body_sha256
import json

from agent.conversation_loop import (
    _bestplan_receipt_metadata,
    _run_bestplan_host_branch,
)


def test_bestplan_turn_metadata_uses_current_receipt_version():
    outcome = {
        "run_id": "run-v2",
        "body": "final plan",
    }

    metadata = _bestplan_receipt_metadata(outcome)

    assert metadata == {
        "bestplan_receipt_version": RECEIPT_VERSION,
        "run_id": "run-v2",
        "body_sha256": body_sha256("final plan"),
    }


def test_bestplan_host_boundary_sanitizes_invalid_config(monkeypatch, caplog):
    import agent.bestplan_orchestrator as orchestrator

    sentinel = "SENTINEL_SECRET"

    def fail_validation(*_args, **_kwargs):
        raise orchestrator.BestPlanUnavailable(
            f"unknown config key: {sentinel}"
        )

    monkeypatch.setattr(orchestrator, "run_bestplan", fail_validation)

    with caplog.at_level("ERROR"):
        outcome = _run_bestplan_host_branch(
            object(),
            "plan it",
            {"config": {sentinel: sentinel}},
        )

    assert outcome == {
        "status": "failed",
        "error": "BestPlan configuration invalid",
        "reason_code": "runtime_invalid",
    }
    assert sentinel not in json.dumps(outcome, sort_keys=True)
    assert sentinel not in caplog.text
