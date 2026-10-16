"""Tests for the authenticated reconnect/replay proof ledger."""

import pytest

from tui_gateway import reconnect_proof


IDENTITY = {"user_id": "sentinel-user", "provider": "sentinel-provider"}


def _begin(tmp_path):
    return reconnect_proof.begin_probe(
        tmp_path,
        operation_id="op-123",
        probe_id="probe-123",
        session_id="runtime-before",
        session_key="persisted-session",
        backend_pid=101,
        replay_epoch="epoch-before",
        last_seen_seq=7,
        auth_identity=IDENTITY,
    )


def test_authenticated_probe_round_trip_is_hash_verified(tmp_path):
    armed = _begin(tmp_path)

    completed = reconnect_proof.complete_probe(
        tmp_path,
        operation_id="op-123",
        probe_id="probe-123",
        session_id="runtime-after",
        session_key="persisted-session",
        backend_pid=202,
        previous_replay_epoch="epoch-before",
        replay_epoch="epoch-after",
        replay_mode="durable_session_history",
        replayed_messages=3,
        auth_identity=IDENTITY,
    )

    assert armed["status"] == "armed"
    assert completed["status"] == "completed"
    assert completed["backend_pid_before"] == 101
    assert completed["backend_pid_after"] == 202
    assert completed["replayed_messages"] == 3
    assert reconnect_proof.read_verified(tmp_path, "op-123") == completed


def test_probe_rejects_missing_or_empty_auth_identity(tmp_path):
    with pytest.raises(reconnect_proof.ReconnectProofError, match="authenticated"):
        reconnect_proof.begin_probe(
            tmp_path,
            operation_id="op-123",
            probe_id="probe-123",
            session_id="runtime-before",
            session_key="persisted-session",
            backend_pid=101,
            replay_epoch="epoch-before",
            last_seen_seq=0,
            auth_identity=None,
        )


def test_completion_rejects_identity_or_epoch_mismatch(tmp_path):
    _begin(tmp_path)

    with pytest.raises(reconnect_proof.ReconnectProofError, match="identity"):
        reconnect_proof.complete_probe(
            tmp_path,
            operation_id="op-123",
            probe_id="probe-123",
            session_id="runtime-after",
            session_key="persisted-session",
            backend_pid=202,
            previous_replay_epoch="epoch-before",
            replay_epoch="epoch-after",
            replay_mode="durable_session_history",
            replayed_messages=1,
            auth_identity={"user_id": "other", "provider": "sentinel-provider"},
        )

    with pytest.raises(reconnect_proof.ReconnectProofError, match="epoch"):
        reconnect_proof.complete_probe(
            tmp_path,
            operation_id="op-123",
            probe_id="probe-123",
            session_id="runtime-after",
            session_key="persisted-session",
            backend_pid=202,
            previous_replay_epoch="epoch-before",
            replay_epoch="epoch-before",
            replay_mode="durable_session_history",
            replayed_messages=1,
            auth_identity=IDENTITY,
        )
