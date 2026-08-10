"""Injected BestPlan promotion-authority interface.

This module intentionally contains no transport, daemon, socket, credential, or
process-launch implementation.  Task-specific code depends on the structural
protocol; the protected authority implementation is supplied by the host.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from agent.bestplan_contract import Enrollment
    from agent.bestplan_source import RepoIdentity


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class AuthorityClientError(RuntimeError):
    """Base exception for injected authority-client failures."""


class AuthorityUnavailable(AuthorityClientError):
    """The authority cannot currently answer a request."""


class AuthorityProtocolError(AuthorityClientError):
    """The authority returned a response outside the frozen client protocol."""


def _nonempty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty string")
    if "\x00" in value:
        raise ValueError(f"{name} contains NUL")
    return value


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _sha256(value: Any, name: str) -> str:
    text = _nonempty(value, name)
    if not _SHA256_RE.fullmatch(text):
        raise ValueError(f"{name} must be a lowercase sha256 digest")
    return text


@dataclass(frozen=True)
class WorkerIdentity:
    """Process identity bound to one short-lived model-broker capability."""

    pid: int
    uid: int
    process_start_id: str
    executable_sha256: str

    def __post_init__(self) -> None:
        _integer(self.pid, "pid", minimum=1)
        _integer(self.uid, "uid")
        _nonempty(self.process_start_id, "process_start_id")
        _sha256(self.executable_sha256, "executable_sha256")


@dataclass(frozen=True, repr=False)
class BrokerCapability:
    """Opaque authority handle; never a provider API credential.

    The handle is deliberately excluded from every human representation.  The
    authority binds it to ``attempt_id`` and ``worker_identity`` and can revoke
    it without exposing any upstream provider material to the worker.
    """

    attempt_id: str
    worker_identity: WorkerIdentity
    opaque_handle: str = field(repr=False)

    def __post_init__(self) -> None:
        _nonempty(self.attempt_id, "attempt_id")
        if not isinstance(self.worker_identity, WorkerIdentity):
            raise ValueError("worker_identity must be a WorkerIdentity")
        _nonempty(self.opaque_handle, "opaque_handle")

    def __repr__(self) -> str:
        return (
            "BrokerCapability("
            f"attempt_id={self.attempt_id!r}, "
            f"worker_identity={self.worker_identity!r}, "
            "opaque_handle=<redacted>)"
        )

    __str__ = __repr__


@dataclass(frozen=True)
class ModelRequest:
    """Credential-free, bounded model request forwarded through the broker."""

    request_id: str
    messages_json: str
    max_output_tokens: int

    def __post_init__(self) -> None:
        _nonempty(self.request_id, "request_id")
        _integer(self.max_output_tokens, "max_output_tokens", minimum=1)
        if not isinstance(self.messages_json, str):
            raise ValueError("messages_json must be a JSON string")
        try:
            messages = json.loads(self.messages_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("messages_json must be valid JSON") from exc
        if not isinstance(messages, list):
            raise ValueError("messages_json must encode a list")


@dataclass(frozen=True)
class ModelResponse:
    """Bounded broker response with accounting but no provider credential."""

    model: str
    content: str
    finish_reason: str
    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        _nonempty(self.model, "model")
        if not isinstance(self.content, str):
            raise ValueError("content must be a string")
        _nonempty(self.finish_reason, "finish_reason")
        _integer(self.input_tokens, "input_tokens")
        _integer(self.output_tokens, "output_tokens")


@dataclass(frozen=True)
class AuthorityStatus:
    """Read-only projection of the authority-owned phase/event pointer."""

    plan_id: str
    execution_protocol: int
    phase: str
    authority_epoch: str
    event_seq: int
    event_hash: str | None
    terminal: bool
    error: str | None = None

    def __post_init__(self) -> None:
        _nonempty(self.plan_id, "plan_id")
        if self.execution_protocol not in (1, 2) or isinstance(
            self.execution_protocol, bool
        ):
            raise ValueError("execution_protocol must be integer 1 or 2")
        _nonempty(self.phase, "phase")
        _nonempty(self.authority_epoch, "authority_epoch")
        _integer(self.event_seq, "event_seq")
        if self.event_hash is not None:
            _sha256(self.event_hash, "event_hash")
        if not isinstance(self.terminal, bool):
            raise ValueError("terminal must be a bool")
        if self.error is not None and not isinstance(self.error, str):
            raise ValueError("error must be a string or None")


@runtime_checkable
class BestplanAuthorityClient(Protocol):
    """Narrow host-injected interface consumed by BestPlan runtime code."""

    def lookup_enrollment(self, repo_identity: "RepoIdentity") -> "Enrollment | None": ...

    def register_model_attempt(
        self,
        attempt_id: str,
        worker_identity: WorkerIdentity,
        model: str,
        request_budget: int,
        token_budget: int,
        expires_at: int,
    ) -> BrokerCapability: ...

    def model_request(
        self, capability: BrokerCapability, request: ModelRequest
    ) -> ModelResponse: ...

    def revoke_model_attempt(self, capability: BrokerCapability) -> None: ...

    def read_authoritative_status(self, plan_id: str) -> AuthorityStatus: ...


class NullAuthorityClient:
    """Default client: no enrollment and no operational authority."""

    def lookup_enrollment(self, repo_identity: "RepoIdentity") -> None:
        del repo_identity
        return None

    def _unavailable(self) -> None:
        raise AuthorityUnavailable("no BestPlan promotion authority is configured")

    def register_model_attempt(
        self,
        attempt_id: str,
        worker_identity: WorkerIdentity,
        model: str,
        request_budget: int,
        token_budget: int,
        expires_at: int,
    ) -> BrokerCapability:
        del attempt_id, worker_identity, model, request_budget, token_budget, expires_at
        self._unavailable()

    def model_request(
        self, capability: BrokerCapability, request: ModelRequest
    ) -> ModelResponse:
        del capability, request
        self._unavailable()

    def revoke_model_attempt(self, capability: BrokerCapability) -> None:
        del capability
        self._unavailable()

    def read_authoritative_status(self, plan_id: str) -> AuthorityStatus:
        del plan_id
        self._unavailable()


class UnavailableAuthorityClient(NullAuthorityClient):
    """Explicit client representing a configured but unreachable authority."""

    def __init__(self, reason: str = "BestPlan promotion authority is unavailable"):
        self.reason = _nonempty(reason, "reason")

    def _unavailable(self) -> None:
        raise AuthorityUnavailable(self.reason)

    def lookup_enrollment(self, repo_identity: "RepoIdentity") -> None:
        del repo_identity
        self._unavailable()
