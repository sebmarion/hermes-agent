"""Durable, authenticated reconnect/replay proof records.

The backend writes one record per externally supplied canary operation. The
record is deliberately an attestation of facts already checked by the server:
WS authentication identity, the attached session, the backend PID, and the
reconnect epoch. It never stores a credential.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Mapping


_OPERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_SESSION_VALUE = 256
_WRITE_LOCK = threading.Lock()


class ReconnectProofError(ValueError):
    """A reconnect proof request is invalid or cannot be advanced safely."""


def _validate_value(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_SESSION_VALUE:
        raise ReconnectProofError(f"invalid {field}")
    return value


def _proof_path(root: Path, operation_id: str) -> Path:
    operation_id = _validate_value(operation_id, "operation_id")
    if not _OPERATION_RE.fullmatch(operation_id):
        raise ReconnectProofError("invalid operation_id")
    root = Path(root).expanduser()
    return root / f"{operation_id}.json"


def _auth_identity_fingerprint(auth_identity: Mapping[str, Any] | None) -> str:
    if not isinstance(auth_identity, Mapping):
        raise ReconnectProofError("authenticated WS identity required")
    user_id = auth_identity.get("user_id")
    provider = auth_identity.get("provider")
    if not isinstance(user_id, str) or not user_id:
        raise ReconnectProofError("authenticated WS identity required")
    if not isinstance(provider, str) or not provider:
        raise ReconnectProofError("authenticated WS identity required")
    raw = json.dumps(
        {"provider": provider, "user_id": user_id},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _require_pid(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 1:
        raise ReconnectProofError(f"invalid {field}")
    return value


def _require_nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReconnectProofError(f"invalid {field}")
    return value


def _require_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReconnectProofError(f"invalid {field}")
    return value


def _ensure_root(root: Path) -> Path:
    root = Path(root).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o750)
    info = root.lstat()
    if not root.is_dir() or root.is_symlink():
        raise ReconnectProofError("reconnect proof directory is unsafe")
    if info.st_mode & 0o002:
        raise ReconnectProofError("reconnect proof directory is writable by others")
    return root


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_transcript(messages: list[Mapping[str, Any]]) -> tuple[bytes, str]:
    """Canonicalize the exact client-visible ordered message array."""
    raw = json.dumps(messages, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return raw, hashlib.sha256(raw).hexdigest()


def make_v2_proof(
    *, operation_id: str, session_id_before: str, session_id_after: str,
    session_key: str, backend_pid_before: int,
    backend_pid_after: int, replay_epoch_before: str, replay_epoch_after: str,
    replay_mode: str, replayed_messages: int,
    transcript_before_sha256: str, transcript_after_sha256: str,
    transcript_before_count: int, transcript_after_count: int,
    auth_identity: Mapping[str, Any],
) -> dict[str, Any]:
    identity = _auth_identity_fingerprint(auth_identity)
    payload = {
        "version": 2, "status": "completed", "operation_id": operation_id,
        "session_id_before": session_id_before,
        "session_id_after": session_id_after, "session_key": session_key,
        "backend_pid_before": backend_pid_before, "backend_pid_after": backend_pid_after,
        "replay_epoch_before": replay_epoch_before, "replay_epoch_after": replay_epoch_after,
        "replay_mode": replay_mode, "replayed_messages": replayed_messages,
        "transcript_before_sha256": transcript_before_sha256,
        "transcript_after_sha256": transcript_after_sha256,
        "transcript_before_count": transcript_before_count,
        "transcript_after_count": transcript_after_count,
        "auth_identity_fingerprint": identity,
    }
    return _with_hash(payload)


def validate_v2_proof(payload: Mapping[str, Any], *, operation_id: str,
                      session_id: str, expected_epoch: str | None = None) -> bool:
    if not isinstance(payload, Mapping):
        raise ReconnectProofError("reconnect proof is not an object")
    required = {"version", "status", "operation_id", "session_id_before",
                "session_id_after", "session_key", "backend_pid_before", "backend_pid_after",
                "replay_epoch_before", "replay_epoch_after", "replay_mode", "replayed_messages",
                "transcript_before_sha256", "transcript_after_sha256", "transcript_before_count",
                "transcript_after_count", "auth_identity_fingerprint", "proof_sha256"}
    if set(payload) != required or payload.get("version") != 2 or payload.get("status") != "completed":
        raise ReconnectProofError("reconnect proof payload shape mismatch")
    if payload.get("operation_id") != operation_id or payload.get("session_id_before") != session_id:
        raise ReconnectProofError("reconnect proof operation/session mismatch")
    for field in ("operation_id", "session_id_before", "session_id_after", "session_key",
                  "replay_epoch_before", "replay_epoch_after"):
        if not isinstance(payload.get(field), str) or not payload[field]:
            raise ReconnectProofError(f"invalid reconnect proof {field}")
    if payload.get("replay_mode") != "durable_session_history":
        raise ReconnectProofError("invalid reconnect proof replay mode")
    for field in ("backend_pid_before", "backend_pid_after", "replayed_messages",
                  "transcript_before_count", "transcript_after_count"):
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ReconnectProofError(f"invalid reconnect proof {field}")
    if payload["replayed_messages"] not in {
        payload["transcript_before_count"], payload["transcript_after_count"]
    }:
        raise ReconnectProofError("reconnect replay count mismatch")
    before, after = payload["transcript_before_sha256"], payload["transcript_after_sha256"]
    if (not isinstance(before, str) or not re.fullmatch(r"[0-9a-f]{64}", before)
            or after != before or payload["transcript_before_count"] != payload["transcript_after_count"]):
        raise ReconnectProofError("reconnect transcript mismatch")
    if payload["backend_pid_before"] == payload["backend_pid_after"] or payload["replay_epoch_before"] == payload["replay_epoch_after"]:
        raise ReconnectProofError("reconnect proof restart evidence missing")
    if expected_epoch is not None and payload["replay_epoch_after"] != expected_epoch:
        raise ReconnectProofError("reconnect proof epoch mismatch")
    actual = payload["proof_sha256"]
    unsigned = dict(payload)
    unsigned.pop("proof_sha256")
    if not isinstance(actual, str) or not re.fullmatch(r"[0-9a-f]{64}", actual) or actual != hashlib.sha256(_canonical_bytes(unsigned)).hexdigest():
        raise ReconnectProofError("reconnect proof hash mismatch")
    if not isinstance(payload["auth_identity_fingerprint"], str) or not re.fullmatch(r"[0-9a-f]{64}", payload["auth_identity_fingerprint"]):
        raise ReconnectProofError("invalid reconnect proof identity")
    return True


def _with_hash(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result["proof_sha256"] = hashlib.sha256(_canonical_bytes(result)).hexdigest()
    return result


def _atomic_write(path: Path, payload: Mapping[str, Any], *, allow_replace: bool) -> dict[str, Any]:
    result = _with_hash(payload)
    encoded = (_canonical_bytes(result) + b"\n")
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with temp.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        if not allow_replace and path.exists():
            raise ReconnectProofError("reconnect proof already exists")
        os.replace(temp, path)
        directory_fd = os.open(str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temp.unlink(missing_ok=True)
    return result


def begin_probe(
    root: Path,
    *,
    operation_id: str,
    probe_id: str,
    session_id: str,
    session_key: str,
    backend_pid: int,
    replay_epoch: str,
    last_seen_seq: int,
    auth_identity: Mapping[str, Any] | None,
    transcript_sha256: str | None = None,
    transcript_count: int | None = None,
) -> dict[str, Any]:
    """Arm a probe from an authenticated transport before a restart."""
    path = _proof_path(_ensure_root(root), operation_id)
    probe_id = _validate_value(probe_id, "probe_id")
    session_id = _validate_value(session_id, "session_id")
    session_key = _validate_value(session_key, "session_key")
    replay_epoch = _validate_value(replay_epoch, "replay_epoch")
    backend_pid = _require_pid(backend_pid, "backend_pid")
    last_seen_seq = _require_nonnegative_int(last_seen_seq, "last_seen_seq")
    identity = _auth_identity_fingerprint(auth_identity)
    if transcript_sha256 is not None or transcript_count is not None:
        if (not isinstance(transcript_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", transcript_sha256)
                or isinstance(transcript_count, bool) or not isinstance(transcript_count, int) or transcript_count <= 0):
            raise ReconnectProofError("reconnect transcript baseline is invalid")
    payload = {
        "version": 2 if transcript_sha256 is not None else 1,
        "status": "armed",
        "operation_id": operation_id,
        "probe_id": probe_id,
        "session_id_before": session_id,
        "session_key": session_key,
        "backend_pid_before": backend_pid,
        "replay_epoch_before": replay_epoch,
        "last_seen_seq_before": last_seen_seq,
        "transcript_before_sha256": transcript_sha256,
        "transcript_before_count": transcript_count,
        "auth_identity_fingerprint": identity,
        "armed_at": time.time(),
    }
    with _WRITE_LOCK:
        if path.exists():
            existing = read_verified(root, operation_id)
            if existing and existing.get("status") == "armed" and all(
                existing.get(key) == payload.get(key)
                for key in (
                    "operation_id",
                    "probe_id",
                    "session_id_before",
                    "session_key",
                    "backend_pid_before",
                    "replay_epoch_before",
                    "auth_identity_fingerprint",
                    "version", "last_seen_seq_before",
                    "transcript_before_sha256", "transcript_before_count",
                )
            ):
                return existing
            raise ReconnectProofError("reconnect proof already exists")
        return _atomic_write(path, payload, allow_replace=False)


def complete_probe(
    root: Path,
    *,
    operation_id: str,
    probe_id: str,
    session_id: str,
    session_key: str,
    backend_pid: int,
    previous_replay_epoch: str,
    replay_epoch: str,
    replay_mode: str,
    replayed_messages: int,
    auth_identity: Mapping[str, Any] | None,
    transcript_before_sha256: str | None = None,
    transcript_after_sha256: str | None = None,
    transcript_before_count: int | None = None,
    transcript_after_count: int | None = None,
) -> dict[str, Any]:
    """Complete a probe after the authenticated client resumes its session."""
    root = _ensure_root(root)
    path = _proof_path(root, operation_id)
    probe_id = _validate_value(probe_id, "probe_id")
    session_id = _validate_value(session_id, "session_id")
    session_key = _validate_value(session_key, "session_key")
    previous_replay_epoch = _validate_value(previous_replay_epoch, "previous_replay_epoch")
    replay_epoch = _validate_value(replay_epoch, "replay_epoch")
    backend_pid = _require_pid(backend_pid, "backend_pid")
    replayed_messages = _require_nonnegative_int(replayed_messages, "replayed_messages")
    if replayed_messages < 1:
        raise ReconnectProofError("reconnect proof has no replayed session history")
    if transcript_before_sha256 is not None:
        transcript_before_count = _require_nonnegative_int(transcript_before_count, "transcript_before_count")
        transcript_after_count = _require_nonnegative_int(transcript_after_count, "transcript_after_count")
        if transcript_before_count <= 0 or transcript_after_count <= 0:
            raise ReconnectProofError("invalid transcript count")
    if replay_mode != "durable_session_history":
        raise ReconnectProofError("unsupported replay mode")
    identity = _auth_identity_fingerprint(auth_identity)
    with _WRITE_LOCK:
        armed = read_verified(root, operation_id)
        if not armed or armed.get("status") != "armed":
            raise ReconnectProofError("reconnect proof is not armed")
        if armed.get("probe_id") != probe_id or armed.get("operation_id") != operation_id:
            raise ReconnectProofError("reconnect proof identity mismatch")
        if armed.get("auth_identity_fingerprint") != identity:
            raise ReconnectProofError("reconnect proof identity mismatch")
        if armed.get("session_key") != session_key:
            raise ReconnectProofError("reconnect proof session mismatch")
        if armed.get("replay_epoch_before") != previous_replay_epoch:
            raise ReconnectProofError("reconnect proof epoch mismatch")
        if previous_replay_epoch == replay_epoch:
            raise ReconnectProofError("reconnect proof epoch did not change")
        before_pid = armed.get("backend_pid_before")
        if not isinstance(before_pid, int) or backend_pid == before_pid:
            raise ReconnectProofError("reconnect proof PID did not change")
        if replayed_messages < 1:
            raise ReconnectProofError("reconnect proof has no replayed session history")
        if transcript_before_sha256 != armed.get("transcript_before_sha256") or transcript_before_count != armed.get("transcript_before_count"):
            raise ReconnectProofError("reconnect transcript before mismatch")
        if transcript_after_sha256 != transcript_before_sha256 or transcript_after_count != transcript_before_count:
            raise ReconnectProofError("reconnect transcript after mismatch")
        payload = make_v2_proof(
            operation_id=operation_id, session_id_before=armed.get("session_id_before"),
            session_id_after=session_id, session_key=session_key,
            backend_pid_before=before_pid, backend_pid_after=backend_pid,
            replay_epoch_before=previous_replay_epoch, replay_epoch_after=replay_epoch,
            replay_mode=replay_mode, replayed_messages=replayed_messages,
            transcript_before_sha256=transcript_before_sha256,
            transcript_after_sha256=transcript_after_sha256,
            transcript_before_count=transcript_before_count,
            transcript_after_count=transcript_after_count, auth_identity=auth_identity,
        )
        if transcript_before_sha256 is not None:
            # Validate before the irreversible armed->completed transition.
            validate_v2_proof(payload, operation_id=operation_id,
                              session_id=armed["session_id_before"])
        payload.pop("proof_sha256")
        if transcript_before_sha256 is None:
            payload = {
                "version": 1, "status": "completed", "operation_id": operation_id, "probe_id": probe_id,
                "session_id_before": armed.get("session_id_before"), "session_id_after": session_id,
                "session_key": session_key, "backend_pid_before": before_pid, "backend_pid_after": backend_pid,
                "replay_epoch_before": previous_replay_epoch, "replay_epoch_after": replay_epoch,
                "last_seen_seq_before": armed.get("last_seen_seq_before"), "replay_mode": replay_mode,
                "replayed_messages": replayed_messages, "auth_identity_fingerprint": identity,
            }
        return _atomic_write(path, payload, allow_replace=True)


def read_verified(root: Path, operation_id: str) -> dict[str, Any] | None:
    """Read one proof and verify its content hash and regular-file status."""
    path = _proof_path(Path(root), operation_id)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if path.is_symlink() or not path.is_file():
        raise ReconnectProofError("reconnect proof file is unsafe")
    if info.st_mode & 0o002:
        raise ReconnectProofError("reconnect proof file is writable by others")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReconnectProofError("reconnect proof is unreadable") from exc
    if not isinstance(payload, dict):
        raise ReconnectProofError("reconnect proof is not an object")
    actual = payload.pop("proof_sha256", None)
    if not isinstance(actual, str) or actual != hashlib.sha256(_canonical_bytes(payload)).hexdigest():
        raise ReconnectProofError("reconnect proof hash mismatch")
    payload["proof_sha256"] = actual
    return payload
