from __future__ import annotations

import hashlib
import importlib
import json
import logging
from collections.abc import Mapping
from types import MappingProxyType

import pytest


def _redaction():
    return importlib.import_module("agent.bestplan_redaction")


def _key(redaction, value: str) -> str:
    return redaction._summary_key(value)


@pytest.mark.parametrize(
    "raw",
    [
        "Authorization: Bearer bearer-secret-value",
        "password=hunter2-value",
        "token: token-secret-value",
        "api_key=sk-test-secret-value",
        "https://alice:userinfo-secret@example.test/path?token=query-secret",
        "github_pat_11AA_secret-material",
        "ghp_secret-material",
        "xoxb-secret-material",
        "AKIAIOSFODNN7EXAMPLE",
        (
            "-----BEGIN PRIVATE KEY-----\n"
            "private-key-secret-material\n"
            "-----END PRIVATE KEY-----"
        ),
    ],
)
def test_common_secret_forms_are_redacted_from_persistable_json(raw):
    redaction = _redaction()

    output = redaction.redact_output(raw, source="check")

    persisted = output.canonical_json
    assert raw not in persisted
    for secret in (
        "bearer-secret-value",
        "hunter2-value",
        "token-secret-value",
        "sk-test-secret-value",
        "userinfo-secret",
        "query-secret",
        "secret-material",
        "AKIAIOSFODNN7EXAMPLE",
        "private-key-secret-material",
    ):
        assert secret not in persisted
    assert output.raw_sha256 == hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_nested_fields_lists_and_bytes_are_redacted_without_losing_raw_digest():
    redaction = _redaction()
    raw = {
        "git": [
            {"authorization": "Bearer nested-secret"},
            "remote https://name:password-secret@example.test/repo.git",
        ],
        "password": "mapping-secret",
        "binary": b"\xffbinary-secret\x00",
        "ok": {"exit_code": 0, "message": "ordinary"},
    }

    output = redaction.redact_output(raw, source="git")
    persisted = output.canonical_json

    assert output.raw_sha256 == hashlib.sha256(
        redaction.canonical_raw_bytes(raw)
    ).hexdigest()
    assert "nested-secret" not in persisted
    assert "password-secret" not in persisted
    assert "mapping-secret" not in persisted
    assert "binary-secret" not in persisted
    decoded = json.loads(persisted)
    assert decoded["source"] == "git"
    ok = decoded["summary"][_key(redaction, "ok")]
    assert ok[_key(redaction, "exit_code")] == 0
    assert ok[_key(redaction, "message")] == {
        "type": "text",
        "size": len("ordinary"),
        "sha256": hashlib.sha256(b"ordinary").hexdigest(),
    }
    binary = decoded["summary"][_key(redaction, "binary")]
    assert binary["type"] == "bytes"
    assert binary["sha256"] == hashlib.sha256(
        raw["binary"]
    ).hexdigest()


def test_raw_bytes_are_hashed_exactly_and_never_decoded_as_fallback_text():
    redaction = _redaction()
    raw = b"\xff\x00Authorization: Bearer binary-secret\xfe"

    output = redaction.redact_output(raw, source="process")

    assert output.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert "binary-secret" not in output.canonical_json
    assert json.loads(output.canonical_json)["summary"] == {
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "type": "bytes",
    }


def test_redaction_is_deterministic_and_domain_separates_summary_digest():
    redaction = _redaction()
    left = redaction.redact_output(
        {"result": "ok", "token": "secret"}, source="review"
    )
    right = redaction.redact_output(
        {"token": "secret", "result": "ok"}, source="review"
    )

    assert left == right
    assert left.summary_sha256 == right.summary_sha256
    assert left.summary_sha256 != hashlib.sha256(
        left.canonical_json.encode("utf-8")
    ).hexdigest()


@pytest.mark.parametrize(
    "value",
    [
        {1: "non-string-key"},
        {"float": 1.25},
        {"set": {"unsupported"}},
        object(),
    ],
)
def test_unserializable_or_ambiguous_payloads_fail_closed(value):
    redaction = _redaction()

    with pytest.raises(redaction.RedactionError):
        redaction.redact_output(value, source="health")


def test_cycles_fail_closed_without_echoing_raw_values():
    redaction = _redaction()
    sentinel = "cycle-secret-value"
    value: list[object] = [sentinel]
    value.append(value)

    with pytest.raises(redaction.RedactionError) as exc_info:
        redaction.redact_output(value, source="canary")

    assert sentinel not in str(exc_info.value)


def test_oversized_raw_and_summary_payloads_are_rejected_before_persistence():
    redaction = _redaction()

    with pytest.raises(redaction.RedactionError, match="raw output exceeds"):
        redaction.redact_output(
            "x" * 65,
            source="model-broker",
            max_raw_bytes=64,
        )
    with pytest.raises(redaction.RedactionError, match="redacted summary exceeds"):
        redaction.redact_output(
            {"ordinary": "x" * 256},
            source="review",
            max_raw_bytes=1024,
            max_summary_bytes=64,
        )


def test_errors_and_logs_never_fall_back_to_raw_payload_repr(caplog):
    redaction = _redaction()
    sentinel = "must-not-enter-log-output"
    value = {"unsafe": object(), "sentinel": sentinel}

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(redaction.RedactionError) as exc_info:
            redaction.redact_output(value, source="process")

    assert sentinel not in str(exc_info.value)
    assert sentinel not in caplog.text


def test_source_labels_are_bounded_and_cannot_carry_secrets():
    redaction = _redaction()

    with pytest.raises(redaction.RedactionError, match="source"):
        redaction.redact_output("ok", source="token=source-secret")
    with pytest.raises(redaction.RedactionError, match="source"):
        redaction.redact_output("ok", source="x" * 65)


def test_raw_encoding_is_type_framed_and_persists_the_raw_kind():
    redaction = _redaction()
    outputs = [
        redaction.redact_output(b"same", source="process"),
        redaction.redact_output("same", source="process"),
        redaction.redact_output(None, source="process"),
        redaction.redact_output(
            {"$bytes": "c2FtZQ==", "$size": 4}, source="process"
        ),
    ]

    assert len({output.raw_framed_sha256 for output in outputs}) == len(outputs)
    assert [json.loads(output.canonical_json)["raw_kind"] for output in outputs] == [
        "bytes",
        "string",
        "null",
        "mapping",
    ]


@pytest.mark.parametrize(
    "key",
    [
        "db_password",
        "refresh_token",
        "AWS_SECRET_ACCESS_KEY",
        "X-API-Key",
        "headers.authorization",
        "env.CLIENT_SECRET",
    ],
)
def test_sensitive_key_variants_redact_their_values(key):
    redaction = _redaction()
    sentinel = "key-variant-secret-value"

    output = redaction.redact_output({key: sentinel}, source="process")

    assert sentinel not in output.canonical_json
    assert (
        json.loads(output.canonical_json)["summary"][_key(redaction, key)]
        == "<redacted>"
    )


def test_secrets_embedded_in_mapping_keys_are_redacted_without_key_collision():
    redaction = _redaction()
    first = "Authorization: Bearer embedded-secret-one"
    second = "token=embedded-secret-two"

    output = redaction.redact_output(
        {first: "one", second: "two"}, source="process"
    )
    summary = json.loads(output.canonical_json)["summary"]

    assert "embedded-secret-one" not in output.canonical_json
    assert "embedded-secret-two" not in output.canonical_json
    assert len(summary) == 2
    assert len(set(summary)) == 2
    assert all(key.startswith("<redacted-key:") for key in summary)


def test_opaque_arbitrary_mapping_keys_are_hashed_directly_and_nested():
    redaction = _redaction()
    sentinel = "opaque-dynamic-key-with-no-secret-marker"

    output = redaction.redact_output(
        {
            sentinel: "direct-value",
            "outer": [{sentinel: "nested-value"}],
        },
        source="process",
    )

    assert sentinel not in output.canonical_json
    assert "direct-value" not in output.canonical_json
    assert "nested-value" not in output.canonical_json


class _ExplodingMapping(Mapping):
    def __iter__(self):
        raise RuntimeError("iterator-leak-secret")

    def __len__(self):
        return 1

    def __getitem__(self, key):
        raise RuntimeError("getitem-leak-secret")


def test_custom_mappings_and_iterator_errors_fail_with_constant_nonchained_errors(caplog):
    redaction = _redaction()

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(redaction.RedactionError) as exc_info:
            redaction.redact_output(_ExplodingMapping(), source="process")

    assert "secret" not in str(exc_info.value)
    assert "secret" not in caplog.text
    assert exc_info.value.__cause__ is None


@pytest.mark.parametrize(
    ("value", "kwargs", "message"),
    [
        ([[[["too-deep"]]]], {"max_depth": 3}, "depth"),
        ([0, 1, 2, 3], {"max_nodes": 4}, "node"),
        ("x" * 17, {"max_scalar_bytes": 16}, "scalar"),
        (10**20, {"max_integer_digits": 20}, "integer"),
    ],
)
def test_structural_limits_fail_during_normalization(value, kwargs, message):
    redaction = _redaction()

    with pytest.raises(redaction.RedactionError, match=message):
        redaction.redact_output(value, source="process", **kwargs)


def test_summary_is_deeply_immutable_and_canonical_json_remains_authoritative():
    redaction = _redaction()
    raw = {"nested": [{"ordinary": "value"}]}

    output = redaction.redact_output(raw, source="process")
    raw["nested"][0]["ordinary"] = "mutated-after-boundary"

    assert isinstance(output.summary, MappingProxyType)
    assert isinstance(output.summary[_key(redaction, "nested")], tuple)
    assert "mutated-after-boundary" not in output.canonical_json
    with pytest.raises(TypeError):
        output.summary["new"] = "not-allowed"


def test_unstructured_text_and_unknown_fields_persist_only_hash_size_metadata():
    redaction = _redaction()
    sentinel = "opaque-unique-value-without-a-secret-marker"

    direct = redaction.redact_output(sentinel, source="process")
    nested = redaction.redact_output({"vendor_blob": sentinel}, source="process")

    assert sentinel not in direct.canonical_json
    assert sentinel not in nested.canonical_json
    assert json.loads(direct.canonical_json)["summary"] == {
        "type": "text",
        "size": len(sentinel.encode()),
        "sha256": hashlib.sha256(sentinel.encode()).hexdigest(),
    }
    assert json.loads(nested.canonical_json)["summary"][
        _key(redaction, "vendor_blob")
    ]["sha256"]


@pytest.mark.parametrize(
    "field",
    [
        "code",
        "delegation_id",
        "error",
        "finish_reason",
        "message",
        "model",
        "ordinary",
        "phase",
        "reason",
        "result",
        "service",
        "status",
        "summary",
        "target",
    ],
)
def test_opaque_text_in_generic_mapping_fields_is_never_allowlisted(field):
    redaction = _redaction()
    sentinel = "opaque-common-field-value"

    direct = redaction.redact_output({field: sentinel}, source="model-broker")
    nested = redaction.redact_output(
        {"outer": [{field: sentinel}]}, source="model-broker"
    )
    expected = {
        "type": "text",
        "size": len(sentinel),
        "sha256": hashlib.sha256(sentinel.encode()).hexdigest(),
    }

    assert sentinel not in direct.canonical_json
    assert sentinel not in nested.canonical_json
    assert json.loads(direct.canonical_json)["summary"][_key(redaction, field)] == expected
    assert json.loads(nested.canonical_json)["summary"][_key(redaction, "outer")][0][
        _key(redaction, field)
    ] == expected


def test_generated_redacted_key_namespace_cannot_collide_with_literal_user_key():
    redaction = _redaction()
    embedded = "token=embedded-key-value"
    generated = "<redacted-key:" + hashlib.sha256(embedded.encode()).hexdigest() + ">"

    output = redaction.redact_output(
        {embedded: "first", generated: "second"}, source="process"
    )
    summary = json.loads(output.canonical_json)["summary"]

    assert len(summary) == 2
    assert summary[_key(redaction, embedded)] == "<redacted>"
    assert _key(redaction, generated) in summary
    assert any(key.startswith("<redacted-key:") for key in summary)
    assert any(key.startswith("<literal-key:") for key in summary)


def test_already_redacted_projection_cannot_be_ingested_again():
    redaction = _redaction()
    first = redaction.redact_output({"status": "ok"}, source="process")

    with pytest.raises(redaction.RedactionError, match="already-redacted"):
        redaction.redact_output(first.canonical_json, source="process")
    with pytest.raises(redaction.RedactionError, match="already-redacted"):
        redaction.redact_output(first, source="process")


def test_raw_byte_limit_is_exact_at_the_boundary():
    redaction = _redaction()

    output = redaction.redact_output(
        "abcd", source="process", max_raw_bytes=4, max_summary_bytes=1024
    )
    assert output.raw_size == 4
    with pytest.raises(redaction.RedactionError, match="size"):
        redaction.redact_output(
            "abcd", source="process", max_raw_bytes=3, max_summary_bytes=1024
        )


@pytest.mark.parametrize("raw", ["x" * (64 * 1024), b"x" * (64 * 1024)])
def test_opaque_scalar_budget_uses_actual_redacted_summary_size(raw):
    redaction = _redaction()

    output = redaction.redact_output(raw, source="process")

    expected = raw if isinstance(raw, bytes) else raw.encode()
    assert output.raw_size == len(expected)
    assert output.raw_sha256 == hashlib.sha256(expected).hexdigest()
    assert len(output.canonical_json.encode("ascii")) < redaction.DEFAULT_MAX_SUMMARY_BYTES
    assert redaction.canonical_raw_bytes(raw) == expected


def test_raw_hash_and_summary_derive_from_one_private_snapshot(monkeypatch):
    redaction = _redaction()
    raw = {"message": "before"}
    original = redaction._summary_value

    def mutate_after_snapshot(node, **kwargs):
        raw["message"] = "after"
        return original(node, **kwargs)

    monkeypatch.setattr(redaction, "_summary_value", mutate_after_snapshot)
    output = redaction.redact_output(raw, source="process")

    expected = {"message": "before"}
    assert output.raw_sha256 == hashlib.sha256(
        redaction.canonical_raw_bytes(expected)
    ).hexdigest()
    assert json.loads(output.canonical_json)["summary"][_key(redaction, "message")] == {
        "type": "text",
        "size": len("before"),
        "sha256": hashlib.sha256(b"before").hexdigest(),
    }
