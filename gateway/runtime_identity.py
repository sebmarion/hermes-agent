"""Immutable, non-secret identity receipt for a running Hermes gateway.

Managed releases inject a fixed build tuple into the gateway launch
environment.  This module snapshots that tuple once, at process import, and
never falls back to the mutable checkout, git metadata, or on-disk manifests.
That makes ``/health/detailed`` suitable for proving that the process serving
traffic is the same sealed Agent/runtime pair selected by the control plane.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import os
import re
import sys
from collections.abc import Mapping
from typing import Any

from gateway.status import get_process_start_token


RUNTIME_IDENTITY_SCHEMA = "hermes.runtime_identity.v1"
PUBLIC_RELEASE_IDENTITY_SCHEMA = "hermes.public_release_identity.v1"
_PUBLIC_RELEASE_FIELDS = (
    "agent_commit",
    "agent_tree",
    "agent_manifest_sha256",
    "runtime_manifest_sha256",
    "release_pair_id",
    "webui_build_id",
    "webui_commit",
    "webui_tree",
    "webui_manifest_sha256",
    "selector_generation",
    "release_transaction_id",
    "gateway_launchd_label",
)


@dataclass(frozen=True)
class IdentityEnvField:
    """One approved, non-secret launch-environment identity field."""

    logical_name: str
    env_name: str
    value_kind: str = "token"


IDENTITY_ENV_FIELDS: tuple[IdentityEnvField, ...] = (
    IdentityEnvField("agent_commit", "HERMES_AGENT_COMMIT", "oid"),
    IdentityEnvField("agent_tree", "HERMES_AGENT_TREE", "oid"),
    IdentityEnvField("agent_manifest_sha256", "HERMES_AGENT_MANIFEST_SHA256", "sha256"),
    IdentityEnvField("agent_source_path", "HERMES_AGENT_SOURCE_PATH", "path"),
    IdentityEnvField("runtime_manifest_sha256", "HERMES_RUNTIME_MANIFEST_SHA256", "sha256"),
    IdentityEnvField("runtime_path", "HERMES_RUNTIME_PATH", "path"),
    IdentityEnvField("release_pair_id", "HERMES_RELEASE_PAIR_ID"),
    IdentityEnvField("webui_build_id", "HERMES_WEBUI_BUILD_ID"),
    IdentityEnvField("webui_commit", "HERMES_WEBUI_COMMIT", "oid"),
    IdentityEnvField("webui_tree", "HERMES_WEBUI_TREE", "oid"),
    IdentityEnvField("webui_manifest_sha256", "HERMES_WEBUI_MANIFEST_SHA256", "sha256"),
    IdentityEnvField("selector_generation", "HERMES_SELECTOR_GENERATION", "positive_int"),
    IdentityEnvField("release_transaction_id", "HERMES_RELEASE_TRANSACTION_ID"),
    IdentityEnvField("gateway_launchd_label", "HERMES_GATEWAY_LAUNCHD_LABEL", "launchd_label"),
)


_SAFE_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/+\-]*\Z")
_OID_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
_POSITIVE_INT_RE = re.compile(r"[1-9][0-9]*\Z")
_LAUNCHD_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\Z")
_DARWIN_START_TOKEN_RE = re.compile(
    r"darwin-proc:(?P<pid>[1-9][0-9]*):(?P<sec>[1-9][0-9]*):(?P<usec>0|[1-9][0-9]{0,5})\Z"
)
_PROCFS_START_TOKEN_RE = re.compile(
    r"procfs:(?P<pid>[1-9][0-9]*):(?P<ticks>[1-9][0-9]*)\Z"
)
_EPOCH_START_TOKEN_RE = re.compile(
    r"[A-Za-z0-9._-]+-proc:(?P<pid>[1-9][0-9]*):"
    r"(?P<sec>[1-9][0-9]*):(?P<usec>0|[1-9][0-9]{0,5})\Z"
)
_MAX_TOKEN_LENGTH = 1024
_MAX_PATH_LENGTH = 4096


def _valid_identity_value(field: IdentityEnvField, value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if "\x00" in value or "\n" in value or "\r" in value:
        return False
    if field.value_kind == "path":
        return (
            len(value) <= _MAX_PATH_LENGTH
            and os.path.isabs(value)
            and os.path.normpath(value) == value
        )
    if field.value_kind == "oid":
        return _OID_RE.fullmatch(value) is not None
    if field.value_kind == "sha256":
        return _SHA256_RE.fullmatch(value) is not None
    if field.value_kind == "positive_int":
        return _POSITIVE_INT_RE.fullmatch(value) is not None
    if field.value_kind == "launchd_label":
        return _LAUNCHD_LABEL_RE.fullmatch(value) is not None
    return len(value) <= _MAX_TOKEN_LENGTH and _SAFE_TOKEN_RE.fullmatch(value) is not None


def _valid_process_start_token(pid: int, token: object) -> bool:
    if not isinstance(token, str) or not token:
        return False
    for pattern in (
        _DARWIN_START_TOKEN_RE,
        _PROCFS_START_TOKEN_RE,
        _EPOCH_START_TOKEN_RE,
    ):
        match = pattern.fullmatch(token)
        if match is None:
            continue
        if int(match.group("pid")) != pid:
            return False
        usec = match.groupdict().get("usec")
        return usec is None or int(usec) < 1_000_000
    return False


def sealed_identity_environment(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return validated identity variables safe to copy into a supervisor.

    Missing and invalid values are intentionally omitted.  The gateway health
    receipt will expose the corresponding logical fields as unverified rather
    than silently deriving replacements from mutable local state.
    """

    source = os.environ if environ is None else environ
    result: dict[str, str] = {}
    for field in IDENTITY_ENV_FIELDS:
        value = source.get(field.env_name)
        if _valid_identity_value(field, value):
            result[field.env_name] = value
    return result


def capture_runtime_identity(
    environ: Mapping[str, str] | None = None,
    *,
    pid: int,
    start_token: str | None,
    interpreter: str,
    cwd: str,
) -> dict[str, Any]:
    """Capture one immutable runtime-identity receipt from launch inputs."""

    source = os.environ if environ is None else environ
    sealed: dict[str, str | None] = {}
    field_status: dict[str, str] = {}
    missing_fields: list[str] = []
    invalid_fields: list[str] = []

    for field in IDENTITY_ENV_FIELDS:
        raw = source.get(field.env_name)
        if _valid_identity_value(field, raw):
            sealed[field.logical_name] = raw
            field_status[field.logical_name] = "verified"
            continue
        sealed[field.logical_name] = None
        field_status[field.logical_name] = "unverified"
        missing_fields.append(field.logical_name)
        if raw not in (None, ""):
            invalid_fields.append(field.logical_name)

    pid_verified = isinstance(pid, int) and not isinstance(pid, bool) and pid > 1
    if not pid_verified:
        missing_fields.append("process.pid")
    start_token_verified = pid_verified and _valid_process_start_token(pid, start_token)
    if not start_token_verified:
        start_token = None
        missing_fields.append("process.start_token")

    return {
        "schema": RUNTIME_IDENTITY_SCHEMA,
        "verified": not missing_fields,
        "sealed": sealed,
        "field_status": field_status,
        "missing_fields": missing_fields,
        "invalid_fields": invalid_fields,
        "process": {
            "pid": pid,
            "start_token": start_token,
            "start_token_status": (
                "verified" if start_token_verified else "unverified"
            ),
            "interpreter": interpreter,
            "cwd": cwd,
            "launchd_label": sealed["gateway_launchd_label"],
        },
    }


def process_identity_matches(
    receipt: Mapping[str, Any],
    *,
    pid: int,
    start_token: str | None,
) -> bool:
    """Match an independently observed ``(pid, start_time)`` pair.

    A recycled PID has a different kernel process-start value and therefore
    cannot match the process that emitted the receipt.
    """

    process = receipt.get("process")
    if not isinstance(process, Mapping):
        return False
    if start_token is None or process.get("start_token_status") != "verified":
        return False
    return process.get("pid") == pid and process.get("start_token") == start_token


def public_release_identity(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Project a full identity receipt into the path-free public contract."""

    sealed = receipt.get("sealed")
    if not isinstance(sealed, Mapping):
        sealed = {}
    process = receipt.get("process")
    if not isinstance(process, Mapping):
        process = {}

    release = {name: sealed.get(name) for name in _PUBLIC_RELEASE_FIELDS}
    public_process = {
        "pid": process.get("pid"),
        "start_token": process.get("start_token"),
        "start_token_status": process.get("start_token_status"),
    }
    verified = (
        receipt.get("verified") is True
        and all(value is not None for value in release.values())
        and public_process["start_token_status"] == "verified"
        and public_process["pid"] is not None
        and public_process["start_token"] is not None
    )
    return {
        "schema": PUBLIC_RELEASE_IDENTITY_SCHEMA,
        "verified": verified,
        "release": release,
        "process": public_process,
    }


_PROCESS_PID = os.getpid()
_PROCESS_START_TOKEN = get_process_start_token(_PROCESS_PID)
try:
    _PROCESS_CWD = os.getcwd()
except OSError:
    _PROCESS_CWD = ""

_CAPTURED_RUNTIME_IDENTITY = capture_runtime_identity(
    os.environ,
    pid=_PROCESS_PID,
    start_token=_PROCESS_START_TOKEN,
    interpreter=sys.executable,
    cwd=_PROCESS_CWD,
)


def runtime_identity_receipt() -> dict[str, Any]:
    """Return a copy of the process-start receipt; callers cannot mutate it."""

    return copy.deepcopy(_CAPTURED_RUNTIME_IDENTITY)


def public_release_identity_receipt() -> dict[str, Any]:
    """Return the immutable public release receipt without local paths."""

    return public_release_identity(_CAPTURED_RUNTIME_IDENTITY)
