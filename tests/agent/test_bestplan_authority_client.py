from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from agent.bestplan_authority_client import (
    AuthorityStatus,
    AuthorityUnavailable,
    BestplanAuthorityClient,
    BrokerCapability,
    ModelRequest,
    ModelResponse,
    NullAuthorityClient,
    UnavailableAuthorityClient,
    WorkerIdentity,
)


def _worker() -> WorkerIdentity:
    return WorkerIdentity(
        pid=123,
        uid=501,
        process_start_id="proc-start-1",
        executable_sha256="a" * 64,
    )


def test_authority_protocol_accepts_a_structural_fake():
    class FakeAuthority:
        def lookup_enrollment(self, repo_identity):
            return None

        def register_model_attempt(
            self, attempt_id, worker_identity, model, request_budget, token_budget, expires_at
        ):
            return BrokerCapability("attempt-1", worker_identity, "opaque-handle")

        def model_request(self, capability, request):
            return ModelResponse(
                model="test/model",
                content="ok",
                finish_reason="stop",
                input_tokens=2,
                output_tokens=1,
            )

        def revoke_model_attempt(self, capability):
            return None

        def read_authoritative_status(self, plan_id):
            return AuthorityStatus(
                plan_id=plan_id,
                execution_protocol=2,
                phase="candidate_ready",
                authority_epoch="epoch-1",
                event_seq=1,
                event_hash="b" * 64,
                terminal=False,
            )

    assert isinstance(FakeAuthority(), BestplanAuthorityClient)


def test_capability_repr_and_str_never_expose_opaque_handle():
    worker = _worker()
    capability = BrokerCapability("attempt-1", worker, "provider-like-secret-value")

    assert "provider-like-secret-value" not in repr(capability)
    assert "provider-like-secret-value" not in str(capability)
    assert "redacted" in repr(capability).lower()
    assert capability.worker_identity == worker
    with pytest.raises(FrozenInstanceError):
        capability.attempt_id = "changed"


def test_worker_identity_is_strict_and_frozen():
    worker = _worker()
    with pytest.raises(FrozenInstanceError):
        worker.pid = 999
    with pytest.raises(ValueError, match="pid"):
        WorkerIdentity(0, 501, "start", "a" * 64)
    with pytest.raises(ValueError, match="sha256"):
        WorkerIdentity(1, 501, "start", "not-a-digest")
    with pytest.raises(ValueError, match="NUL"):
        WorkerIdentity(1, 501, "start\x00suffix", "a" * 64)


def test_model_request_and_response_reject_floats_and_invalid_budgets():
    request = ModelRequest(
        request_id="request-1",
        messages_json='[{"role":"user","content":"hi"}]',
        max_output_tokens=32,
    )
    assert request.max_output_tokens == 32
    with pytest.raises(ValueError, match="max_output_tokens"):
        ModelRequest("request-1", "[]", 1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="tokens"):
        ModelResponse("m", "ok", "stop", -1, 1)


def test_null_and_unavailable_clients_have_no_operational_authority():
    null = NullAuthorityClient()
    assert isinstance(null, BestplanAuthorityClient)
    assert null.lookup_enrollment(object()) is None

    unavailable = UnavailableAuthorityClient("authority is offline")
    with pytest.raises(AuthorityUnavailable, match="offline"):
        unavailable.lookup_enrollment(object())

    for client in (null, unavailable):
        with pytest.raises(AuthorityUnavailable):
            client.register_model_attempt(
                "attempt-1", _worker(), "model", 1, 100, 123456
            )
        with pytest.raises(AuthorityUnavailable):
            client.read_authoritative_status("bp_1")
