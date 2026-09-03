"""One-operation loopback credential for the reconnect canary."""
from __future__ import annotations

import hmac
import json
import math
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CANARY_RPC_ALLOWLIST = frozenset({
    "session.resume", "session.events.since", "session.reconnect.probe", "session.reconnect.ack",
})
OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_DESCRIPTOR_BYTES = 4096

@dataclass(frozen=True)
class CredentialDescriptor:
    operation_id: str
    session_id: str
    token: str
    expires_at: float


_MAX_TTL_SECONDS = 300.0


def create_descriptor(root: Path, *, operation_id: str, session_id: str, ttl_seconds: float, now: float | None = None) -> Path:
    if (not isinstance(operation_id, str) or not OPERATION_ID_RE.fullmatch(operation_id)
            or not isinstance(session_id, str) or not session_id
            or isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float))
            or not math.isfinite(ttl_seconds) or not 0 < ttl_seconds <= _MAX_TTL_SECONDS):
        raise ValueError("invalid credential descriptor fields")
    root = Path(root)
    existed = root.exists() or root.is_symlink()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root_info = root.lstat()
    if root.is_symlink() or not stat.S_ISDIR(root_info.st_mode):
        raise ValueError("unsafe credential root")
    if existed and stat.S_IMODE(root_info.st_mode) != 0o700:
        raise ValueError("unsafe credential root")
    if stat.S_IMODE(root_info.st_mode) != 0o700:
        os.chmod(root, 0o700)
    token = secrets.token_urlsafe(32)
    path = root / f"{operation_id}.json"
    payload = {"operation_id": operation_id, "session_id": session_id, "token": token,
               "expires_at": (time.time() if now is None else now) + ttl_seconds}
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush(); os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def load_descriptor(path: Path, *, now: float | None = None) -> CredentialDescriptor:
    path = Path(path)
    root = path.parent
    root_info = root.lstat()
    if root.is_symlink() or not stat.S_ISDIR(root_info.st_mode) or stat.S_IMODE(root_info.st_mode) != 0o700:
        raise ValueError("unsafe credential root")
    if not OPERATION_ID_RE.fullmatch(path.stem) or path.suffix != ".json":
        raise ValueError("invalid credential descriptor path")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            fd = os.open(path.name, flags, dir_fd=root_fd)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > MAX_DESCRIPTOR_BYTES:
                    raise ValueError("unsafe credential descriptor")
                raw = b""
                while len(raw) <= MAX_DESCRIPTOR_BYTES:
                    chunk = os.read(fd, MAX_DESCRIPTOR_BYTES + 1 - len(raw))
                    if not chunk:
                        break
                    raw += chunk
                if len(raw) > MAX_DESCRIPTOR_BYTES:
                    raise ValueError("credential descriptor is too large")
            finally:
                os.close(fd)
        finally:
            os.close(root_fd)
    except OSError as exc:
        raise ValueError("unsafe credential descriptor") from exc
    payload: Any = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid credential descriptor")
    if set(payload) != {"operation_id", "session_id", "token", "expires_at"}:
        raise ValueError("invalid credential descriptor")
    try:
        op, sid, token, expiry = payload["operation_id"], payload["session_id"], payload["token"], payload["expires_at"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid credential descriptor") from exc
    if (not isinstance(op, str) or not OPERATION_ID_RE.fullmatch(op) or
        not all(isinstance(v, str) and v and len(v) <= 512 for v in (sid, token)) or
        isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or
        not math.isfinite(expiry) or expiry <= (time.time() if now is None else now) or
        expiry > (time.time() if now is None else now) + _MAX_TTL_SECONDS):
        raise ValueError("invalid credential descriptor")
    return CredentialDescriptor(op, sid, token, expiry)


def validate_descriptor(path: Path, token: str, *, peer: str, now: float | None = None) -> bool:
    if peer not in {"127.0.0.1", "::1"} or not isinstance(token, str):
        return False
    try:
        descriptor = load_descriptor(path, now=now)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return hmac.compare_digest(token.encode("utf-8"), descriptor.token.encode("utf-8"))
