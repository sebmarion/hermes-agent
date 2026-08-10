"""Bounded secret-safe serialization for BestPlan proof projections.

One ingress traversal converts exact safe builtins into an immutable typed
snapshot.  Exact raw hashes and the persistable redacted summary are derived
only from that snapshot, so a mutable/custom input cannot diverge between the
two representations.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


REDACTED_OUTPUT_SCHEMA = "hermes.bestplan.redacted-output.v1"
DEFAULT_MAX_RAW_BYTES = 1024 * 1024
DEFAULT_MAX_SUMMARY_BYTES = 32 * 1024
DEFAULT_MAX_DEPTH = 32
DEFAULT_MAX_NODES = 4096
DEFAULT_MAX_SCALAR_BYTES = 256 * 1024
DEFAULT_MAX_INTEGER_DIGITS = 64

_RAW_FRAME_DOMAIN = b"hermes.bestplan.raw-output.v1\0"
_SUMMARY_DOMAIN = b"hermes.bestplan.redacted-output.v1\0"
_SOURCE_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SUMMARY_KEY_RE = re.compile(
    r"^<(?:key|redacted-key|literal-key):[0-9a-f]{64}>$"
)
_REDACTED_ENVELOPE_KEYS = frozenset(
    {
        "schema",
        "source",
        "raw_kind",
        "raw_sha256",
        "raw_framed_sha256",
        "raw_size",
        "summary",
    }
)
_RAW_KINDS = frozenset(
    {"null", "boolean", "integer", "string", "bytes", "list", "mapping"}
)
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE)-----.*?"
    r"-----END [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE)-----",
    re.DOTALL,
)
_AUTH_RE = re.compile(
    r"(?i)(\bauthorization\s*[:=]\s*(?:bearer|basic)\s+)[^\s,;]+"
)
_KEY_VALUE_RE = re.compile(
    r"(?i)(\b(?:password|passwd|pwd|token|api[-_]?key|secret|"
    r"client[-_]?secret|refresh[-_]?token|access[-_]?key|cookie)"
    r"\b\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s&,;]+)"
)
_URL_USERINFO_RE = re.compile(r"(?i)(\bhttps?://)[^/@\s]+@")
_PROVIDER_TOKEN_RE = re.compile(
    r"(?i)\b(?:github_pat_[a-z0-9_-]+|gh[pousr]_[a-z0-9_-]+|"
    r"xox[a-z]-[a-z0-9_-]+|sk-[a-z0-9_-]{8,})\b"
)
_AWS_ACCESS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_SENSITIVE_KEY_TOKENS = frozenset(
    {
        "authorization",
        "password",
        "passwd",
        "pwd",
        "token",
        "secret",
        "cookie",
        "apikey",
        "api_key",
        "accesskey",
        "access_key",
        "privatekey",
        "private_key",
    }
)
class RedactionError(ValueError):
    """A raw value cannot be safely represented for persistence."""


@dataclass(frozen=True)
class RedactedOutput:
    source: str
    raw_kind: str
    raw_sha256: str
    raw_framed_sha256: str
    raw_size: int
    summary: Any
    canonical_json: str
    summary_sha256: str


@dataclass(frozen=True)
class _Node:
    kind: str
    value: Any


@dataclass
class _Budget:
    max_raw_bytes: int
    max_summary_bytes: int | None
    max_depth: int
    max_nodes: int
    max_scalar_bytes: int
    max_integer_digits: int
    nodes: int = 0
    raw_estimate: int = 0
    summary_estimate: int = 0

    def add_node(
        self,
        *,
        depth: int,
        scalar_bytes: int = 0,
        summary_bytes: int | None = None,
    ) -> None:
        if depth > self.max_depth:
            raise RedactionError("raw output exceeds the configured depth limit")
        self.nodes += 1
        if self.nodes > self.max_nodes:
            raise RedactionError("raw output exceeds the configured node limit")
        if scalar_bytes > self.max_scalar_bytes:
            raise RedactionError("raw output exceeds the configured scalar limit")
        # Conservative framing overhead prevents unbounded materialization.  A
        # final exact assertion remains as a defensive invariant only.
        self.raw_estimate += scalar_bytes + (8 if scalar_bytes == 0 else 0)
        self.summary_estimate += (
            scalar_bytes + 16 if summary_bytes is None else summary_bytes
        )
        if self.raw_estimate > self.max_raw_bytes:
            raise RedactionError("raw output exceeds the configured size limit")
        if (
            self.max_summary_bytes is not None
            and self.summary_estimate > self.max_summary_bytes
        ):
            raise RedactionError("redacted summary exceeds the configured size limit")


def _positive(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RedactionError(f"{name} must be a positive integer")
    return value


def _normalize(
    value: Any,
    *,
    budget: _Budget,
    depth: int,
    active: set[int],
) -> _Node:
    value_type = type(value)
    if value is None:
        budget.add_node(depth=depth)
        return _Node("null", None)
    if value_type is bool:
        budget.add_node(depth=depth)
        return _Node("boolean", value)
    if value_type is int:
        digits = len(str(abs(value)))
        if digits > budget.max_integer_digits:
            raise RedactionError("raw output exceeds the configured integer limit")
        budget.add_node(depth=depth, scalar_bytes=digits + (1 if value < 0 else 0))
        return _Node("integer", value)
    if value_type is str:
        try:
            encoded = value.encode("utf-8", "strict")
        except UnicodeEncodeError:
            raise RedactionError("raw output contains an invalid string") from None
        budget.add_node(
            depth=depth,
            scalar_bytes=len(encoded),
            summary_bytes=160,
        )
        return _Node("string", value)
    if value_type is bytes:
        budget.add_node(
            depth=depth,
            scalar_bytes=len(value),
            summary_bytes=160,
        )
        return _Node("bytes", value)
    if value_type is list:
        identity = id(value)
        if identity in active:
            raise RedactionError("raw output contains a cycle")
        budget.add_node(depth=depth)
        active.add(identity)
        try:
            items = tuple(
                _normalize(
                    item,
                    budget=budget,
                    depth=depth + 1,
                    active=active,
                )
                for item in value
            )
        except RedactionError:
            raise
        except BaseException:
            raise RedactionError("raw output changed during normalization") from None
        finally:
            active.remove(identity)
        return _Node("list", items)
    if value_type is dict:
        identity = id(value)
        if identity in active:
            raise RedactionError("raw output contains a cycle")
        budget.add_node(depth=depth)
        active.add(identity)
        try:
            entries: list[tuple[str, _Node]] = []
            for key, item in value.items():
                if type(key) is not str:
                    raise RedactionError("raw output mappings require string keys")
                try:
                    encoded_key = key.encode("utf-8", "strict")
                except UnicodeEncodeError:
                    raise RedactionError("raw output contains an invalid mapping key") from None
                budget.add_node(
                    depth=depth + 1,
                    scalar_bytes=len(encoded_key),
                    summary_bytes=96,
                )
                entries.append(
                    (
                        key,
                        _normalize(
                            item,
                            budget=budget,
                            depth=depth + 1,
                            active=active,
                        ),
                    )
                )
        except RedactionError:
            raise
        except BaseException:
            raise RedactionError("raw output changed during normalization") from None
        finally:
            active.remove(identity)
        entries.sort(key=lambda pair: pair[0])
        return _Node("mapping", tuple(entries))
    # Reject every custom mapping, iterator, scalar subclass, and object before
    # invoking user-defined methods or representations.
    raise RedactionError("raw output contains an unsupported value type")


def _typed_value(node: _Node) -> Any:
    if node.kind == "null":
        return {"kind": "null"}
    if node.kind == "boolean":
        return {"kind": "boolean", "value": node.value}
    if node.kind == "integer":
        return {"kind": "integer", "value": str(node.value)}
    if node.kind == "string":
        return {"kind": "string", "value": node.value}
    if node.kind == "bytes":
        return {"kind": "bytes", "hex": node.value.hex()}
    if node.kind == "list":
        return {"kind": "list", "items": [_typed_value(item) for item in node.value]}
    if node.kind == "mapping":
        return {
            "kind": "mapping",
            "entries": [
                {"key": key, "value": _typed_value(item)}
                for key, item in node.value
            ],
        }
    raise AssertionError("unknown normalized node")


def _raw_content(node: _Node) -> bytes:
    # Preserve exact ordinary raw bytes for byte/string command output so the
    # familiar sha256 remains independently reproducible.  ``raw_kind`` plus
    # ``raw_framed_sha256`` prevents cross-type collisions.
    if node.kind == "bytes":
        return node.value
    if node.kind == "string":
        return node.value.encode("utf-8")
    return json.dumps(
        _typed_value(node),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _redact_text(value: str) -> str:
    redacted = _PEM_RE.sub("<redacted-pem>", value)
    redacted = _URL_USERINFO_RE.sub(r"\1<redacted>@", redacted)
    redacted = _AUTH_RE.sub(r"\1<redacted>", redacted)
    redacted = _KEY_VALUE_RE.sub(r"\1<redacted>", redacted)
    redacted = _PROVIDER_TOKEN_RE.sub("<redacted-token>", redacted)
    return _AWS_ACCESS_KEY_RE.sub("<redacted-access-key>", redacted)


def _sensitive_descriptor(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
    collapsed = normalized.replace("_", "")
    tokens = set(normalized.split("_"))
    return bool(
        tokens & _SENSITIVE_KEY_TOKENS
        or collapsed in _SENSITIVE_KEY_TOKENS
        or any(
            marker in normalized
            for marker in (
                "password",
                "refresh_token",
                "client_secret",
                "access_key",
                "private_key",
                "api_key",
                "authorization",
            )
        )
    )


def _text_metadata(value: str) -> dict[str, Any]:
    encoded = value.encode("utf-8")
    return {
        "type": "text",
        "size": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _summary_key(key: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    if key.startswith(("<key:", "<redacted-key:", "<literal-key:")):
        namespace = "literal-key"
    elif _sensitive_descriptor(key) or _redact_text(key) != key:
        namespace = "redacted-key"
    else:
        namespace = "key"
    return f"<{namespace}:{digest}>"


def _summary_value(node: _Node) -> Any:
    if node.kind in {"null", "boolean", "integer"}:
        return node.value
    if node.kind == "string":
        return _text_metadata(node.value)
    if node.kind == "bytes":
        return {
            "type": "bytes",
            "size": len(node.value),
            "sha256": hashlib.sha256(node.value).hexdigest(),
        }
    if node.kind == "list":
        return [_summary_value(item) for item in node.value]
    if node.kind == "mapping":
        result: dict[str, Any] = {}
        for key, item in node.value:
            safe_key = _summary_key(key)
            if safe_key in result:
                raise RedactionError("redacted mapping keys collide")
            result[safe_key] = (
                "<redacted>"
                if _sensitive_descriptor(key)
                else _summary_value(item)
            )
        return result
    raise AssertionError("unknown normalized node")


def _deep_freeze(value: Any) -> Any:
    if type(value) is dict:
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_deep_freeze(item) for item in value)
    return value


def _snapshot(
    value: Any,
    *,
    max_raw_bytes: int,
    max_summary_bytes: int | None,
    max_depth: int,
    max_nodes: int,
    max_scalar_bytes: int,
    max_integer_digits: int,
) -> _Node:
    budget = _Budget(
        max_raw_bytes=_positive(max_raw_bytes, "max_raw_bytes"),
        max_summary_bytes=(
            None
            if max_summary_bytes is None
            else _positive(max_summary_bytes, "max_summary_bytes")
        ),
        max_depth=_positive(max_depth, "max_depth"),
        max_nodes=_positive(max_nodes, "max_nodes"),
        max_scalar_bytes=_positive(max_scalar_bytes, "max_scalar_bytes"),
        max_integer_digits=_positive(max_integer_digits, "max_integer_digits"),
    )
    try:
        return _normalize(value, budget=budget, depth=0, active=set())
    except RedactionError:
        raise
    except BaseException:
        raise RedactionError("raw output normalization failed") from None


def canonical_raw_bytes(value: Any) -> bytes:
    """Return the reproducible raw-content bytes for a safe builtin value."""

    node = _snapshot(
        value,
        max_raw_bytes=DEFAULT_MAX_RAW_BYTES,
        max_summary_bytes=None,
        max_depth=DEFAULT_MAX_DEPTH,
        max_nodes=DEFAULT_MAX_NODES,
        max_scalar_bytes=DEFAULT_MAX_SCALAR_BYTES,
        max_integer_digits=DEFAULT_MAX_INTEGER_DIGITS,
    )
    return _raw_content(node)


def validate_redacted_projection(
    value: Any,
    *,
    max_summary_bytes: int = DEFAULT_MAX_SUMMARY_BYTES,
) -> dict[str, Any]:
    """Validate that persisted JSON is exactly serializer-producible output."""

    max_summary_bytes = _positive(max_summary_bytes, "max_summary_bytes")
    if type(value) is not str:
        raise RedactionError("redacted projection must be canonical JSON text")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        raise RedactionError("redacted projection must be ASCII JSON") from None
    if not 1 <= len(encoded) <= max_summary_bytes:
        raise RedactionError("redacted projection exceeds the configured size limit")
    try:
        envelope = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise RedactionError("redacted projection JSON is invalid") from None
    if type(envelope) is not dict or frozenset(envelope) != _REDACTED_ENVELOPE_KEYS:
        raise RedactionError("redacted projection envelope shape is invalid")
    try:
        canonical = json.dumps(
            envelope,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise RedactionError("redacted projection JSON is invalid") from None
    if canonical != value:
        raise RedactionError("redacted projection JSON is not canonical")
    if envelope["schema"] != REDACTED_OUTPUT_SCHEMA:
        raise RedactionError("redacted projection schema is unsupported")
    if type(envelope["source"]) is not str or not _SOURCE_RE.fullmatch(
        envelope["source"]
    ):
        raise RedactionError("redacted projection source is invalid")
    raw_kind = envelope["raw_kind"]
    if type(raw_kind) is not str or raw_kind not in _RAW_KINDS:
        raise RedactionError("redacted projection raw kind is unsupported")
    for name in ("raw_sha256", "raw_framed_sha256"):
        digest = envelope[name]
        if type(digest) is not str or not _SHA256_RE.fullmatch(digest):
            raise RedactionError("redacted projection digest is invalid")
    raw_size = envelope["raw_size"]
    if (
        type(raw_size) is not int
        or raw_size < 0
        or raw_size > DEFAULT_MAX_RAW_BYTES
    ):
        raise RedactionError("redacted projection raw size is invalid")

    nodes = 0

    def validate_summary(item: Any, *, depth: int, redacted_allowed: bool) -> str:
        nonlocal nodes
        nodes += 1
        if nodes > DEFAULT_MAX_NODES or depth > DEFAULT_MAX_DEPTH:
            raise RedactionError("redacted projection summary is out of bounds")
        if item is None:
            return "null"
        if type(item) is bool:
            return "boolean"
        if type(item) is int:
            if len(str(abs(item))) > DEFAULT_MAX_INTEGER_DIGITS:
                raise RedactionError("redacted projection integer is out of bounds")
            return "integer"
        if type(item) is str:
            if redacted_allowed and item == "<redacted>":
                return "redacted"
            raise RedactionError("redacted projection contains raw text")
        if type(item) is list:
            for child in item:
                validate_summary(child, depth=depth + 1, redacted_allowed=False)
            return "list"
        if type(item) is not dict:
            raise RedactionError("redacted projection summary type is invalid")
        if frozenset(item) == {"type", "size", "sha256"}:
            metadata_type = item["type"]
            size = item["size"]
            digest = item["sha256"]
            if type(metadata_type) is not str or metadata_type not in {"text", "bytes"}:
                raise RedactionError("redacted projection metadata type is invalid")
            if (
                type(size) is not int
                or size < 0
                or size > DEFAULT_MAX_SCALAR_BYTES
            ):
                raise RedactionError("redacted projection metadata size is invalid")
            if type(digest) is not str or not _SHA256_RE.fullmatch(digest):
                raise RedactionError("redacted projection metadata digest is invalid")
            return "string" if metadata_type == "text" else "bytes"
        for key, child in item.items():
            if type(key) is not str or not _SUMMARY_KEY_RE.fullmatch(key):
                raise RedactionError("redacted projection mapping key is invalid")
            validate_summary(child, depth=depth + 1, redacted_allowed=True)
        return "mapping"

    summary_kind = validate_summary(
        envelope["summary"], depth=0, redacted_allowed=False
    )
    if summary_kind != raw_kind:
        raise RedactionError("redacted projection summary kind differs")
    return envelope


def redact_output(
    value: Any,
    *,
    source: str,
    max_raw_bytes: int = DEFAULT_MAX_RAW_BYTES,
    max_summary_bytes: int = DEFAULT_MAX_SUMMARY_BYTES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    max_scalar_bytes: int = DEFAULT_MAX_SCALAR_BYTES,
    max_integer_digits: int = DEFAULT_MAX_INTEGER_DIGITS,
) -> RedactedOutput:
    """Digest ``value`` once and return only a bounded redacted projection."""

    if isinstance(value, RedactedOutput):
        raise RedactionError("already-redacted output cannot be ingested")
    if type(value) is str and REDACTED_OUTPUT_SCHEMA in value:
        try:
            possible_projection = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            possible_projection = None
        if (
            type(possible_projection) is dict
            and possible_projection.get("schema") == REDACTED_OUTPUT_SCHEMA
        ):
            raise RedactionError("already-redacted output cannot be ingested")
    if not isinstance(source, str) or not _SOURCE_RE.fullmatch(source):
        raise RedactionError("source must be a bounded lowercase label")
    node = _snapshot(
        value,
        max_raw_bytes=max_raw_bytes,
        max_summary_bytes=max_summary_bytes,
        max_depth=max_depth,
        max_nodes=max_nodes,
        max_scalar_bytes=max_scalar_bytes,
        max_integer_digits=max_integer_digits,
    )
    raw_content = _raw_content(node)
    if len(raw_content) > max_raw_bytes:
        raise RedactionError("raw output exceeds the configured size limit")
    raw_sha256 = hashlib.sha256(raw_content).hexdigest()
    raw_framed_sha256 = hashlib.sha256(
        _RAW_FRAME_DOMAIN
        + node.kind.encode("ascii")
        + b"\0"
        + len(raw_content).to_bytes(8, "big")
        + raw_content
    ).hexdigest()
    summary_plain = _summary_value(node)
    envelope = {
        "schema": REDACTED_OUTPUT_SCHEMA,
        "source": source,
        "raw_kind": node.kind,
        "raw_sha256": raw_sha256,
        "raw_framed_sha256": raw_framed_sha256,
        "raw_size": len(raw_content),
        "summary": summary_plain,
    }
    encoded = json.dumps(
        envelope,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(encoded.encode("ascii")) > max_summary_bytes:
        raise RedactionError("redacted summary exceeds the configured size limit")
    summary_sha256 = hashlib.sha256(
        _SUMMARY_DOMAIN + encoded.encode("ascii")
    ).hexdigest()
    return RedactedOutput(
        source=source,
        raw_kind=node.kind,
        raw_sha256=raw_sha256,
        raw_framed_sha256=raw_framed_sha256,
        raw_size=len(raw_content),
        summary=_deep_freeze(summary_plain),
        canonical_json=encoded,
        summary_sha256=summary_sha256,
    )
