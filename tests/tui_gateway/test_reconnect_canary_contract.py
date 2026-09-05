from __future__ import annotations

import hashlib
import json
import os
from types import SimpleNamespace
from pathlib import Path

import pytest

from tui_gateway import reconnect_auth, reconnect_proof


class _CanaryTransport:
    def __init__(self, scope):
        self.auth_scope = scope
        self.auth_identity = {"user_id": "sentinel-user", "provider": "sentinel-provider"}
        self.frames = []

    def write(self, frame):
        self.frames.append(frame)
        return True


def test_scoped_credential_descriptor_is_safe_and_constant_time(tmp_path: Path):
    descriptor = reconnect_auth.create_descriptor(
        tmp_path, operation_id="op-1", session_id="stored-1", ttl_seconds=30, now=100.0
    )
    assert descriptor.stat().st_mode & 0o777 == 0o600
    loaded = reconnect_auth.load_descriptor(descriptor, now=101.0)
    assert loaded.operation_id == "op-1"
    assert loaded.session_id == "stored-1"
    assert reconnect_auth.validate_descriptor(descriptor, loaded.token, peer="127.0.0.1", now=101.0)
    assert not reconnect_auth.validate_descriptor(descriptor, "wrong", peer="127.0.0.1", now=101.0)
    assert not reconnect_auth.validate_descriptor(descriptor, loaded.token, peer="10.0.0.1", now=101.0)


def test_canary_transport_allows_only_exact_rpc_set():
    assert reconnect_auth.CANARY_RPC_ALLOWLIST == frozenset({
        "session.resume", "session.events.since", "session.reconnect.probe", "session.reconnect.ack"
    })


def test_canonical_transcript_preserves_order_and_content():
    messages = [{"role": "user", "content": "é"}, {"role": "assistant", "content": "ok"}]
    raw, digest = reconnect_proof.canonical_transcript(messages)
    assert raw == json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert digest == hashlib.sha256(raw).hexdigest()
    assert digest != reconnect_proof.canonical_transcript(list(reversed(messages)))[1]


def test_proof_v2_requires_equal_positive_transcript_digests_and_counts(tmp_path: Path):
    payload = reconnect_proof.make_v2_proof(
        operation_id="op-1", session_id_before="stored-1", session_id_after="runtime-1",
        session_key="stored-1", backend_pid_before=10,
        backend_pid_after=11, replay_epoch_before="a", replay_epoch_after="b",
        replay_mode="durable_session_history", replayed_messages=2,
        transcript_before_sha256="a" * 64, transcript_after_sha256="a" * 64,
        transcript_before_count=2, transcript_after_count=2,
        auth_identity={"user_id": "u", "provider": "provider"},
    )
    assert reconnect_proof.validate_v2_proof(payload, operation_id="op-1", session_id="stored-1", expected_epoch="b")
    payload["transcript_after_sha256"] = "b" * 64
    with pytest.raises(reconnect_proof.ReconnectProofError):
        reconnect_proof.validate_v2_proof(payload, operation_id="op-1", session_id="stored-1")


def test_descriptor_rejects_traversal_symlink_mode_size_and_expiry(tmp_path: Path):
    with pytest.raises(ValueError):
        reconnect_auth.create_descriptor(tmp_path, operation_id="../escape", session_id="s", ttl_seconds=1, now=1)
    descriptor = reconnect_auth.create_descriptor(tmp_path, operation_id="safe", session_id="s", ttl_seconds=1, now=1)
    descriptor.chmod(0o644)
    with pytest.raises(ValueError):
        reconnect_auth.load_descriptor(descriptor, now=1.5)
    descriptor.chmod(0o600)
    descriptor.write_text("x" * 5000)
    with pytest.raises(ValueError):
        reconnect_auth.load_descriptor(descriptor, now=1.5)
    descriptor.unlink()
    target = tmp_path / "target"
    target.write_text(json.dumps({"operation_id":"safe","session_id":"s","token":"t","expires_at":99}))
    target.chmod(0o600)
    descriptor.symlink_to(target)
    with pytest.raises(ValueError):
        reconnect_auth.load_descriptor(descriptor, now=1)



def test_web_canary_is_only_api_ws_and_loopback(tmp_path: Path, monkeypatch):
    from hermes_cli import web_server_chat as web_server
    descriptor = reconnect_auth.create_descriptor(tmp_path, operation_id="op-1", session_id="s", ttl_seconds=30)
    loaded = reconnect_auth.load_descriptor(descriptor)
    monkeypatch.setenv("HERMES_RECONNECT_CREDENTIAL_DIR", str(tmp_path))
    from hermes_cli.web_server import app
    monkeypatch.setattr(app.state, "auth_required", False, raising=False)

    def ws(path, peer, query=None):
        return SimpleNamespace(url=SimpleNamespace(path=path), client=SimpleNamespace(host=peer),
                               headers={"X-Hermes-Canary-Operation":"op-1", "X-Hermes-Canary-Token":loaded.token},
                               query_params=query or {})
    accepted = ws("/api/ws", "127.0.0.1")
    assert web_server._ws_auth_reason(accepted)[0] is None
    assert web_server._ws_auth_reason(ws("/api/console", "127.0.0.1"))[0] == "canary_invalid"
    assert web_server._ws_auth_reason(ws("/api/ws", "10.0.0.1"))[0] == "canary_invalid"
    assert web_server._ws_auth_reason(ws("/api/ws", "127.0.0.1", {"canary": loaded.token}))[0] == "canary_invalid"


def test_real_session_resume_captures_exact_display_projection(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    from tui_gateway import server

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("stored-1", source="tui")
    db.append_message("stored-1", "user", "exact")
    monkeypatch.setattr(server, "_db", db)
    monkeypatch.setattr(server, "_db_error", None)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda sid: None)
    transport = _CanaryTransport({
        "kind": "promotion-canary", "operation_id": "op-1", "session_id": "stored-1",
    })
    try:
        assert server.dispatch({"id": 1, "method": "session.resume", "params": {
            "session_id": "stored-1", "operation_id": "op-1"}}, transport) is None
        for _ in range(200):
            if transport.frames:
                break
            import time
            time.sleep(0.01)
        assert transport.frames and "result" in transport.frames[0], transport.frames
        result = transport.frames[0]["result"]
        assert result["messages"]
        _, digest = reconnect_proof.canonical_transcript(result["messages"])
        assert transport.auth_scope["runtime_session_id"] == result["session_id"]
        assert transport.auth_scope["resume_transcript_sha256"] == digest
        assert transport.auth_scope["resume_transcript_count"] == len(result["messages"])
    finally:
        server._sessions.pop(transport.auth_scope.get("runtime_session_id", ""), None)
        db.close()


def test_real_ack_handler_retries_the_exact_completed_proof(tmp_path, monkeypatch):
    from tui_gateway import event_replay, server

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    epoch = ["epoch-before"]
    monkeypatch.setattr(event_replay, "replay_epoch", lambda: epoch[0])
    pid = [101, 202]
    monkeypatch.setattr(__import__("os"), "getpid", lambda: pid.pop(0) if len(pid) > 1 else pid[0])
    _, digest = reconnect_proof.canonical_transcript([{"role": "user", "content": "exact"}])
    transport = _CanaryTransport({
        "kind": "promotion-canary", "operation_id": "op-1", "session_id": "stored-1",
        "runtime_session_id": "runtime-1", "resume_transcript_sha256": digest,
        "resume_transcript_count": 1,
    })
    server._sessions["runtime-1"] = {
        "transport": transport, "session_key": "stored-1", "resume_session_id": "stored-1",
    }
    try:
        armed = server.dispatch({"id": 1, "method": "session.reconnect.probe", "params": {
            "operation_id": "op-1", "session_id": "runtime-1", "probe_id": "probe-1",
            "replay_epoch": "epoch-before", "last_seen_seq": 0,
            "transcript_sha256": digest, "transcript_count": 1}}, transport)
        assert armed.get("result", {}).get("status") == "armed", armed
        epoch[0] = "epoch-after"
        params = {"operation_id": "op-1", "session_id": "runtime-1", "probe_id": "probe-1",
                  "previous_replay_epoch": "epoch-before", "replay_mode": "durable_session_history",
                  "replayed_messages": 1, "transcript_before_sha256": digest,
                  "transcript_after_sha256": digest, "transcript_before_count": 1,
                  "transcript_after_count": 1}
        first = server.dispatch({"id": 2, "method": "session.reconnect.ack", "params": params}, transport)
        assert "result" in first
        mismatch = dict(params, replay_epoch="epoch-after", transcript_after_sha256="0" * 64)
        rejected = server.dispatch({"id": 3, "method": "session.reconnect.ack", "params": mismatch}, transport)
        assert rejected["error"]["code"] == 4091
        retry = dict(params, replay_epoch="epoch-after")
        second = server.dispatch({"id": 4, "method": "session.reconnect.ack", "params": retry}, transport)
        assert second["result"] == first["result"]
    finally:
        server._sessions.pop("runtime-1", None)


def test_completed_retry_validates_proof_against_authenticated_stored_session(
    tmp_path, monkeypatch
):
    from tui_gateway import event_replay, server

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(event_replay, "replay_epoch", lambda: "epoch-after")
    monkeypatch.setattr(__import__("os"), "getpid", lambda: 202)
    _, digest = reconnect_proof.canonical_transcript([{"role": "user", "content": "exact"}])
    transport = _CanaryTransport({
        "kind": "promotion-canary", "operation_id": "op-1", "session_id": "stored-1",
        "runtime_session_id": "runtime-1", "resume_transcript_sha256": digest,
        "resume_transcript_count": 1,
    })
    server._sessions["runtime-1"] = {
        "transport": transport, "session_key": "stored-1", "resume_session_id": "stored-1",
    }
    params = {
        "operation_id": "op-1", "session_id": "runtime-1", "probe_id": "probe-1",
        "previous_replay_epoch": "epoch-before", "replay_mode": "durable_session_history",
        "replayed_messages": 1, "transcript_before_sha256": digest,
        "transcript_after_sha256": digest, "transcript_before_count": 1,
        "transcript_after_count": 1, "replay_epoch": "epoch-after",
    }
    try:
        proof = reconnect_proof.make_v2_proof(
            operation_id="op-1", session_id_before="wrong-stored", session_id_after="runtime-1",
            session_key="stored-1", backend_pid_before=101, backend_pid_after=202,
            replay_epoch_before="epoch-before", replay_epoch_after="epoch-after",
            replay_mode="durable_session_history", replayed_messages=1,
            transcript_before_sha256=digest, transcript_after_sha256=digest,
            transcript_before_count=1, transcript_after_count=1,
            auth_identity=transport.auth_identity,
        )
        root = tmp_path / "runtime" / "reconnect-proofs"
        root.mkdir(parents=True)
        proof.pop("proof_sha256", None)
        reconnect_proof._atomic_write(root / "op-1.json", proof, allow_replace=False)
        rejected = server.dispatch({"id": 1, "method": "session.reconnect.ack", "params": params}, transport)
        assert rejected["error"]["code"] == 4091
    finally:
        server._sessions.pop("runtime-1", None)


@pytest.mark.parametrize("field,value", [
    ("replayed_messages", True), ("replayed_messages", "1"),
    ("transcript_before_count", False), ("transcript_before_count", "1"),
    ("transcript_after_count", True), ("transcript_after_count", "1"),
])
def test_ack_rejects_boolean_and_string_counts_before_coercion(tmp_path, monkeypatch, field, value):
    from tui_gateway import event_replay, server

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(event_replay, "replay_epoch", lambda: "epoch-before")
    _, digest = reconnect_proof.canonical_transcript([{"role": "user", "content": "exact"}])
    transport = _CanaryTransport({
        "kind": "promotion-canary", "operation_id": "op-1", "session_id": "stored-1",
        "runtime_session_id": "runtime-1", "resume_transcript_sha256": digest,
        "resume_transcript_count": 1,
    })
    server._sessions["runtime-1"] = {"transport": transport, "session_key": "stored-1"}
    params = {
        "operation_id": "op-1", "session_id": "runtime-1", "probe_id": "probe-1",
        "previous_replay_epoch": "epoch-before", "replay_mode": "durable_session_history",
        "replayed_messages": 1, "transcript_before_sha256": digest,
        "transcript_after_sha256": digest, "transcript_before_count": 1,
        "transcript_after_count": 1,
    }
    params[field] = value
    try:
        response = server.dispatch({"id": 1, "method": "session.reconnect.ack", "params": params}, transport)
        assert response["error"]["code"] == 4000
    finally:
        server._sessions.pop("runtime-1", None)


def test_probe_rejects_boolean_and_string_last_seen_seq(tmp_path, monkeypatch):
    from tui_gateway import event_replay, server

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(event_replay, "replay_epoch", lambda: "epoch-before")
    _, digest = reconnect_proof.canonical_transcript([])
    transport = _CanaryTransport({
        "kind": "promotion-canary", "operation_id": "op-1", "session_id": "runtime-1",
        "runtime_session_id": "runtime-1", "resume_transcript_sha256": digest,
        "resume_transcript_count": 0,
    })
    server._sessions["runtime-1"] = {"transport": transport, "session_key": "stored-1"}
    try:
        for value in (True, "0"):
            response = server.dispatch({"id": 1, "method": "session.reconnect.probe", "params": {
                "operation_id": "op-1", "session_id": "runtime-1", "probe_id": "probe-1",
                "replay_epoch": "epoch-before", "last_seen_seq": value,
                "transcript_sha256": digest, "transcript_count": 0}}, transport)
            assert response["error"]["code"] == 4000
    finally:
        server._sessions.pop("runtime-1", None)


def test_real_dispatch_rejects_non_canary_and_wrong_bindings():
    from tui_gateway import server

    ordinary = _CanaryTransport({})
    server._sessions["ordinary-runtime"] = {"transport": ordinary, "session_key": "stored-1"}
    assert server.dispatch({"id": 1, "method": "session.reconnect.probe", "params": {
        "session_id": "ordinary-runtime"
    }}, ordinary)["error"]["code"] == 4030
    server._sessions.pop("ordinary-runtime", None)
    canary = _CanaryTransport({"kind": "promotion-canary", "operation_id": "op-1", "session_id": "stored-1"})
    assert server.dispatch({"id": 2, "method": "session.events.since", "params": {"operation_id": "other", "session_id": "stored-1"}}, canary)["error"]["code"] == 4030
    assert server.dispatch({"id": 3, "method": "session.events.since", "params": {"operation_id": "op-1", "session_id": "wrong"}}, canary)["error"]["code"] == 4030


def test_concurrent_canary_resume_rejected_while_first_is_in_flight(monkeypatch):
    import threading
    from tui_gateway import server

    submitted = threading.Event()
    release = threading.Event()
    original = server._methods["session.resume"]

    def controlled_resume(rid, params):
        submitted.set()
        assert release.wait(timeout=2)
        return {"id": rid, "result": {"session_id": "runtime-bound", "messages": []}}

    import threading
    monkeypatch.setitem(server._methods, "session.resume", controlled_resume)
    transport = _CanaryTransport({
        "kind": "promotion-canary", "operation_id": "op-1", "session_id": "stored-1",
    })
    server._sessions["placeholder"] = {"transport": transport, "session_key": "stored-1"}
    try:
        assert server.dispatch({"id": 1, "method": "session.resume", "params": {
            "operation_id": "op-1", "session_id": "stored-1"}}, transport) is None
        assert submitted.wait(timeout=2)
        second = server.dispatch({"id": 2, "method": "session.resume", "params": {
            "operation_id": "op-1", "session_id": "stored-1"}}, transport)
        assert second["error"]["code"] == 4092
        release.set()
        for _ in range(200):
            if transport.frames:
                break
            import time
            time.sleep(0.01)
        assert transport.auth_scope.get("resume_pending") is False
        assert transport.auth_scope.get("runtime_session_id") == "runtime-bound"
        permanently_rejected = server.dispatch({"id": 3, "method": "session.resume", "params": {
            "operation_id": "op-1", "session_id": "stored-1"}}, transport)
        assert permanently_rejected["error"]["code"] == 4030
    finally:
        release.set()
        server._sessions.pop("placeholder", None)
        server._sessions.pop("runtime-bound", None)
        monkeypatch.setitem(server._methods, "session.resume", original)
