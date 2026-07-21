from agent.bestplan_orchestrator import (
    RECEIPT_BEGIN, RECEIPT_END, append_receipt, body_sha256, make_receipt,
    normalize_count, quorum_for, reconcile_bestplan_receipts, validate_receipt,
    validate_runtime,
)


def test_count_and_quorum():
    assert normalize_count(1) == 2
    assert normalize_count(9) == 5
    assert [quorum_for(n) for n in range(2, 6)] == [2, 2, 3, 4]


def test_runtime_is_openai_sol_only():
    cfg = validate_runtime()
    assert (cfg["provider"], cfg["model"], cfg["runtime_route"]) == ("openai-codex", "gpt-5.6-sol", "codex_responses")


def test_receipt_has_canonical_markers_and_hash():
    body = "plan body"
    receipt = make_receipt("run-1", model="gpt-5.6-sol", quorum="3/3", synth_status="success", body=body)
    assert receipt.startswith(RECEIPT_BEGIN)
    assert receipt.endswith(RECEIPT_END)
    assert validate_receipt(receipt, body)
    assert not validate_receipt(receipt, body + "!")
    assert body_sha256(body)


def test_append_and_reconcile_is_idempotent(tmp_path):
    path = tmp_path / "receipts.jsonl"
    append_receipt(path, {"run_id": "run-1", "status": "running"})
    assert reconcile_bestplan_receipts(path) == ["run-1"]
    assert reconcile_bestplan_receipts(path) == []
