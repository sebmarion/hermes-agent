"""Durable terminal-outbox JSON-RPC handlers."""

from .method_ctx import HandlerRegistry

_registry = HandlerRegistry()
method = _registry.method


@method("terminal.outbox.pending")
def _(rid, params: dict) -> dict:
    sid = str(params.get("session_id") or "")
    transport = current_transport()
    from tools import async_delegation

    with _sessions_lock:
        session = _sessions.get(sid)
        if not sid or session is None or session.get("transport") is not transport:
            return _err(rid, 4010, "attached session required")
        claims = session.setdefault("_terminal_outbox_claims", {})
        claim_entry = claims.get(id(transport))
        if claim_entry is None or claim_entry[0] is not transport:
            claim_entry = (transport, f"terminal-outbox:{uuid.uuid4().hex}")
            claims[id(transport)] = claim_entry
        claim_id = claim_entry[1]
        stable_session_id = str(
            session.get("resume_session_id") or session.get("session_key") or sid
        )
        profile_home = session.get("profile_home")
        home_token = set_hermes_home_override(profile_home) if profile_home else None
        try:
            rows = async_delegation.claim_terminal_outputs(stable_session_id, claim_id)
            if rows:
                live_claims = session.get("_terminal_outbox_live_claims", {}).get(id(transport), {})
                for row in rows:
                    live_claims.pop(str(row.get("delivery_id") or ""), None)
        finally:
            if home_token is not None:
                reset_hermes_home_override(home_token)
    return _ok(
        rid,
        {
            "session_id": sid,
            "deliveries": [
                async_delegation.replay_terminal_output(row, sid) for row in rows
            ],
        },
    )


@method("terminal.outbox.ack")
def _(rid, params: dict) -> dict:
    sid = str(params.get("session_id") or "")
    transport = current_transport()
    delivery_id = str(params.get("delivery_id") or "")
    if not delivery_id:
        return _err(rid, 4004, "delivery_id required")
    from tools import async_delegation

    with _sessions_lock:
        session = _sessions.get(sid)
        if not sid or session is None or session.get("transport") is not transport:
            return _err(rid, 4010, "attached session required")
        claims = session.setdefault("_terminal_outbox_claims", {})
        claim_entry = claims.get(id(transport))
        if claim_entry is None or claim_entry[0] is not transport:
            claim_entry = (transport, f"terminal-outbox:{uuid.uuid4().hex}")
            claims[id(transport)] = claim_entry
        claim_id = claim_entry[1]
        live_claim = (
            session.get("_terminal_outbox_live_claims", {})
            .get(id(transport), {})
            .get(delivery_id)
        )
        ack_claim_id = live_claim if isinstance(live_claim, str) else claim_id
        stable_session_id = str(
            session.get("resume_session_id") or session.get("session_key") or sid
        )
        profile_home = session.get("profile_home")
        home_token = set_hermes_home_override(profile_home) if profile_home else None
        try:
            acknowledged = async_delegation.ack_terminal_output(
                delivery_id, stable_session_id, ack_claim_id
            )
            # Keep the live publisher claim bound to this transport after ACK.
            # The durable ACK is idempotent, and a repeated ACK on the same
            # transport must not mint a new claim. It is replaced only when the
            # transport/session binding is torn down or replay is taken over.
        finally:
            if home_token is not None:
                reset_hermes_home_override(home_token)
    return _ok(rid, {"session_id": sid, "delivery_id": delivery_id, "acknowledged": acknowledged})


def register(server) -> None:
    _registry.install(server)
