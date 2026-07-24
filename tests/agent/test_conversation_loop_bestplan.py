from agent.bestplan_orchestrator import RECEIPT_VERSION, body_sha256
from agent.conversation_loop import _bestplan_receipt_metadata


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
