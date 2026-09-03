#!/usr/bin/env python3
"""Restart hermes-backend only when the core checkout advanced.

Runs as an ExecStartPost of hermes-upstream-merge.service (root), after the
deployment coordinator has activated the gateway. Idempotent: records the
promoted commit SHA and restarts the backend only when HEAD moved. Git inspects
the user-owned checkout as seb so systemd's root context cannot trigger Git's
safe-directory rejection.
"""
from __future__ import annotations

import pathlib
import json
import subprocess
import sys

REPO = "/home/seb/projects/hermes-agent"
REPO_USER = "seb"
STATE = pathlib.Path("/var/lib/hermes-deployment/backend-promoted.sha")
SERVICE = "hermes-backend.service"
ACTIVE_SESSIONS = pathlib.Path("/home/seb/.hermes/runtime/active_sessions.json")
BACKEND_PORT = ":9119"


def _active_client_counts() -> tuple[int, int]:
    """Return active Desktop/TUI leases and established backend connections."""
    try:
        payload = json.loads(ACTIVE_SESSIONS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read active session registry: {exc}") from exc

    entries = payload.get("entries", []) if isinstance(payload, dict) else []
    if not isinstance(entries, list):
        raise RuntimeError("active session registry has an invalid entries list")
    desktop_tui = sum(
        1
        for entry in entries
        if isinstance(entry, dict) and str(entry.get("surface", "")).lower() in {"desktop", "tui"}
    )
    try:
        connections = subprocess.run(
            ["/usr/bin/ss", "-Htn", "state", "established"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(f"cannot inspect backend connections: {exc}") from exc
    if connections.returncode:
        raise RuntimeError("cannot inspect backend connections")
    established = sum(1 for line in connections.stdout.splitlines() if BACKEND_PORT in line)
    return desktop_tui, established


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv

    proc = subprocess.run(
        [
            "/usr/sbin/runuser",
            "-u",
            REPO_USER,
            "--",
            "/usr/bin/git",
            "-C",
            REPO,
            "rev-parse",
            "HEAD",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode:
        print(f"cannot read {REPO} HEAD: {proc.stderr.strip()}", file=sys.stderr)
        return 1
    head = proc.stdout.strip()

    if "--defer-if-connected" in argv:
        try:
            desktop_tui, connections = _active_client_counts()
        except RuntimeError as exc:
            print(json.dumps({"deferred": True, "reason": str(exc)}))
            return 1
        if desktop_tui or connections:
            reason = (
                "backend has an active Desktop/TUI session; disconnect clients before promotion"
                if desktop_tui
                else "backend has established client connections; disconnect clients before promotion"
            )
            print(json.dumps({"deferred": True, "reason": reason}))
            return 0

    last = STATE.read_text(encoding="utf-8").strip() if STATE.exists() else ""
    if head == last:
        print(f"hermes-backend already running {head[:12]}")
        return 0

    if dry_run:
        print(f"WOULD restart {SERVICE}: {last[:12] or '(none)'} -> {head[:12]}")
        return 0

    restart = subprocess.run(["systemctl", "restart", SERVICE])
    if restart.returncode:
        print(f"{SERVICE} restart exited {restart.returncode}", file=sys.stderr)
        return 1

    active = subprocess.run(["systemctl", "is-active", "--quiet", SERVICE])
    if active.returncode:
        print(f"{SERVICE} not active after restart", file=sys.stderr)
        return 1

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(head + "\n", encoding="utf-8")
    print(f"{SERVICE} restarted at {head[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
