"""A changed or incomplete canary may not seal a durable success record."""
import pytest
from tui_gateway import reconnect_proof as rp

@pytest.fixture
def armed(tmp_path):
    args = dict(operation_id="op-test", probe_id="probe", session_id="stored",
                session_key="stored", backend_pid=123, replay_epoch="before",
                last_seen_seq=0, auth_identity={"provider":"canary","user_id":"owner"},
                transcript_sha256="a"*64, transcript_count=4)
    rp.begin_probe(tmp_path, **args)
    return tmp_path, args

@pytest.mark.parametrize("change", [{"transcript_sha256":"b"*64},
    {"transcript_count":5}, {"last_seen_seq":1}])
def test_arming_retry_requires_exact_baseline(armed, change):
    root, args = armed
    before = (root / "op-test.json").read_bytes()
    with pytest.raises(rp.ReconnectProofError): rp.begin_probe(root, **{**args, **change})
    assert (root / "op-test.json").read_bytes() == before

def test_incomplete_replay_does_not_seal_success(armed):
    root, args = armed
    with pytest.raises(rp.ReconnectProofError):
        rp.complete_probe(root, operation_id="op-test", probe_id="probe", session_id="runtime",
            session_key="stored", backend_pid=456, previous_replay_epoch="before", replay_epoch="after",
            replay_mode="durable_session_history", replayed_messages=1,
            auth_identity=args["auth_identity"], transcript_before_sha256="a"*64,
            transcript_after_sha256="a"*64, transcript_before_count=4, transcript_after_count=4)
    assert rp.read_verified(root,"op-test")["status"] == "armed"
