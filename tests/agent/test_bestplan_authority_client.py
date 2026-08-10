from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from agent.bestplan_authority_client import (
    AuthorityStatus,
    AuthorityUnavailable,
    BestplanAuthorityClient,
    BrokerCapability,
    BrokerTurnRequest,
    BrokerTurnResponse,
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


def test_broker_turn_envelope_round_trips_tools_and_tool_calls_canonically():
    request_body = {
        "messages": [{"role": "user", "content": "edit the file"}],
        "model": "test/model",
        "tool_choice": "auto",
        "tools": [{
            "type": "function",
            "function": {
                "name": "terminal",
                "description": "Run one command",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
    }
    request_json = json.dumps(
        request_body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    request = BrokerTurnRequest(
        request_id="turn-1",
        request_json=request_json,
        max_output_tokens=512,
    )
    assert json.loads(request.request_json)["tools"][0]["function"]["name"] == "terminal"

    response_body = {
        "id": "chatcmpl-broker-1",
        "object": "chat.completion",
        "created": 1,
        "model": "test/model",
        "choices": [{
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": "{}"},
                }],
            },
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }
    response_json = json.dumps(
        response_body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    response = BrokerTurnResponse(
        request_id="turn-1",
        response_json=response_json,
        input_tokens=5,
        output_tokens=3,
    )
    assert json.loads(response.response_json)["choices"][0]["message"]["tool_calls"]


def test_broker_turn_envelope_is_bounded_canonical_and_mapping_only():
    with pytest.raises(ValueError, match="canonical"):
        BrokerTurnRequest("turn-1", '{"model": "m"}', 1)
    with pytest.raises(ValueError, match="object"):
        BrokerTurnRequest("turn-1", "[]", 1)
    with pytest.raises(ValueError, match="bounded"):
        BrokerTurnRequest(
            "turn-1",
            json.dumps({"messages": [], "padding": "x" * (4 * 1024 * 1024)}),
            1,
        )
    with pytest.raises(ValueError, match="request_id"):
        BrokerTurnResponse(
            "turn-2",
            json.dumps({"choices": []}, sort_keys=True, separators=(",", ":")),
            0,
            0,
        ).validate_for_request("turn-1")


@pytest.mark.parametrize("request_id", ["x" * 129, "turn\n1", "türn-1"])
def test_broker_turn_request_identity_is_short_ascii_control_free(request_id):
    with pytest.raises(ValueError, match="request_id"):
        BrokerTurnRequest(request_id, "{}", 1)


def test_broker_turn_envelope_rejects_huge_integers_deep_json_and_inner_frame_overflow():
    huge = 10**100
    with pytest.raises(ValueError, match="max_output_tokens"):
        BrokerTurnRequest("turn-1", "{}", huge)
    with pytest.raises(ValueError, match="input_tokens"):
        BrokerTurnResponse("turn-1", "{}", huge, 0)

    deeply_nested = '{"a":' * 2_000 + "0" + "}" * 2_000
    with pytest.raises(ValueError, match="valid JSON"):
        BrokerTurnRequest("turn-1", deeply_nested, 1)

    oversized_inner = json.dumps(
        {"padding": "x" * (2 * 1024 * 1024)},
        sort_keys=True,
        separators=(",", ":"),
    )
    with pytest.raises(ValueError, match="bounded"):
        BrokerTurnResponse("turn-1", oversized_inner, 0, 0)


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
