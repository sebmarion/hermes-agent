"""Optional local browser broker adapter for concurrency-safe Zeus sessions.

When browser.broker_socket is configured, Hermes leases a browser from the broker
instead of spawning or sharing a browser directly. Explicit operator CDP overrides
still take precedence in browser_tool_session.
"""

import hashlib
import json
import os
import re
import socket
from typing import Any, Dict, Optional

from tools.browser_tool_origin import origin as _bt


def _broker_socket() -> str:
    return _bt._browser_cfg(
        "broker_socket", "", lambda v: str(v or "").strip(),
        "broker_socket from config",
    )


def enabled() -> bool:
    return os.name != "nt" and bool(_broker_socket())


def _send(payload: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
    path = _broker_socket()
    if not path:
        raise RuntimeError("browser broker is not configured")
    data = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect(path)
            sock.sendall(data)
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
        except OSError as exc:
            raise RuntimeError(f"browser broker unavailable: {type(exc).__name__}") from exc
    if not buf:
        raise RuntimeError("browser broker returned no response")
    try:
        result = json.loads(buf.split(b"\n", 1)[0])
    except json.JSONDecodeError as exc:
        raise RuntimeError("browser broker returned invalid response") from exc
    if not result.get("ok"):
        raise RuntimeError(str(result.get("error") or "browser broker request failed"))
    return result


_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

def _configured_session() -> str:
    value = _bt._browser_cfg(
        "broker_session", "", lambda v: str(v or "").strip(),
        "broker_session from config",
    )
    if value and not _SESSION_RE.fullmatch(value):
        raise RuntimeError("invalid browser.broker_session")
    return value

def _session_key(task_id: str) -> str:
    configured = _configured_session()
    if configured:
        return configured
    digest = hashlib.sha256(task_id.encode("utf-8", "replace")).hexdigest()[:20]
    return f"hermes-{digest}"


def acquire(task_id: str, *, persistent: bool = True, ttl: int = 300) -> Dict[str, Any]:
    result = _send({
        "op": "lease_acquire",
        "session": _session_key(task_id),
        "persistent": bool(persistent),
        "ttl": max(30, min(int(ttl), 900)),
    }, timeout=max(45, min(int(ttl), 900) + 15))
    return {
        "session_name": f"broker_{hashlib.sha256(task_id.encode()).hexdigest()[:10]}",
        "bb_session_id": None,
        "cdp_url": str(result["cdp_url"]),
        "features": {"local": True, "browserd": True},
        "browserd_lease_id": str(result["lease_id"]),
        "browserd_session": str(result["session"]),
        "browserd_socket": _broker_socket(),
        "browserd_persistent": bool(persistent),
    }


def renew(session_info: Dict[str, Any], ttl: int = 300) -> bool:
    lease_id = str(session_info.get("browserd_lease_id") or "")
    if not lease_id:
        return False
    _send({"op": "lease_renew", "lease_id": lease_id, "ttl": max(30, min(int(ttl), 900))}, timeout=10)
    return True


def release(session_info: Dict[str, Any], *, close_worker: bool = True) -> None:
    lease_id = str(session_info.get("browserd_lease_id") or "")
    session = str(session_info.get("browserd_session") or "")
    if lease_id:
        try:
            _send({"op": "lease_release", "lease_id": lease_id}, timeout=15)
        except RuntimeError as exc:
            _bt.logger.warning("Could not release browser broker lease: %s", exc)
    if close_worker and session:
        try:
            _send({"op": "close", "session": session}, timeout=20)
        except RuntimeError as exc:
            _bt.logger.warning("Could not close browser broker session: %s", exc)
